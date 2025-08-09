import os
import re
import base64
import subprocess
import tempfile
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set

from nonebot import on_command, on_regex, logger, get_bots
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.plugin import PluginMetadata
from nonebot.permission import SUPERUSER
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER

from .config import Config

# 新增依赖
import json
import random
import difflib

try:
    from pypinyin import lazy_pinyin, Style
    _PYPINYIN_AVAILABLE = True
except Exception:
    _PYPINYIN_AVAILABLE = False

try:
    from nonebot_plugin_apscheduler import scheduler
    _SCHEDULER_AVAILABLE = True
except Exception:
    scheduler = None  # type: ignore
    _SCHEDULER_AVAILABLE = False

__plugin_meta__ = PluginMetadata(
    name="hachimi_music_plugin",
    description="哈基米音乐播放插件",
    usage="""
    命令列表：
    /哈基米帮助 - 查看帮助/指令列表
    /哈基米歌单 - 随机展示30首可播放的音乐
    /哈基米点歌 <序号> - 播放指定序号的音乐（并计入排行榜）
    /来首哈基米 - 随机播放一首音乐（别名：随机哈基米、给我来首哈基米）
    /哈基米搜索 <关键词或正则或ID> - 搜索乐曲（最多返回10条）
    /哈基米排行榜 [页码] - 查看排行榜（每页30条，默认第1页）
    /重载哈基米歌单 - 重新扫描音乐文件（管理员）
    /开启哈基米推送 - 开启每日定时推送（管理员）
    /关闭哈基米推送 - 关闭每日定时推送（管理员）
    /测试哈基米 - 插件自检
    """,
    config=Config,
)

# 音乐文件映射
music_files: Dict[int, Dict[str, str]] = {}
music_data_path = Path(__file__).parent / "music_data"

# 播放计数（按文件名持久化，避免序号变化导致错位）
data_dir = Path(__file__).parent / "data"
data_dir.mkdir(parents=True, exist_ok=True)
play_counts_file = data_dir / "play_counts.json"
play_counts_by_filename: Dict[str, int] = {}

# 推送开启的群聊持久化
push_groups_file = data_dir / "push_groups.json"
enabled_push_groups: Set[int] = set()


def load_play_counts() -> None:
    """加载历史播放计数（按文件名）。"""
    global play_counts_by_filename
    if play_counts_file.exists():
        try:
            with open(play_counts_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    # 仅保留 int 计数
                    play_counts_by_filename = {
                        str(k): int(v) for k, v in data.items() if isinstance(v, (int, float))
                    }
        except Exception as e:
            logger.warning(f"加载播放计数失败，将重置：{e}")
            play_counts_by_filename = {}
    else:
        play_counts_by_filename = {}


def save_play_counts() -> None:
    """安全保存播放计数。"""
    try:
        tmp_path = play_counts_file.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(play_counts_by_filename, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, play_counts_file)
    except Exception as e:
        logger.error(f"保存播放计数失败：{e}")


def load_push_groups() -> None:
    global enabled_push_groups
    if push_groups_file.exists():
        try:
            with open(push_groups_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    enabled_push_groups = {int(gid) for gid in data}
                elif isinstance(data, dict) and "groups" in data:
                    enabled_push_groups = {int(gid) for gid in data.get("groups", [])}
                else:
                    enabled_push_groups = set()
        except Exception as e:
            logger.warning(f"加载推送群列表失败，将重置：{e}")
            enabled_push_groups = set()
    else:
        enabled_push_groups = set()


def save_push_groups() -> None:
    try:
        tmp_path = push_groups_file.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(sorted(list(enabled_push_groups)), f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, push_groups_file)
    except Exception as e:
        logger.error(f"保存推送群列表失败：{e}")


# 文本归一化与匹配工具
_non_cjk_alnum_space = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")
_spaces = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = str(text).lower()
    # 去除特殊符号（保留中文、英文字母和数字）
    text = _non_cjk_alnum_space.sub(" ", text)
    # 合并并去空白
    text = _spaces.sub("", text)
    return text


def text_to_pinyin(text: str) -> str:
    if not _PYPINYIN_AVAILABLE or not text:
        return ""
    try:
        return "".join(lazy_pinyin(text))
    except Exception:
        return ""


def text_to_pinyin_initials(text: str) -> str:
    if not _PYPINYIN_AVAILABLE or not text:
        return ""
    try:
        initials = lazy_pinyin(text, style=Style.FIRST_LETTER)
        return "".join(initials)
    except Exception:
        return ""


def try_regex_match(pattern: str, candidates: List[str]) -> bool:
    if not pattern:
        return False
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except Exception:
        return False
    for c in candidates:
        if not c:
            continue
        if compiled.search(c):
            return True
    return False


def similarity_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(a=a, b=b).ratio()


def compute_match_score(query: str, title: str) -> float:
    if not query or not title:
        return 0.0

    q_norm = normalize_text(query)
    t_norm = normalize_text(title)

    q_py = text_to_pinyin(query)
    t_py = text_to_pinyin(title)

    q_ini = text_to_pinyin_initials(query)
    t_ini = text_to_pinyin_initials(title)

    score = 0.0

    # 正则匹配（对原始、规范化、拼音）
    if try_regex_match(query, [title, t_norm, t_py]):
        score += 1.2

    # 规范化子串匹配
    if q_norm and q_norm in t_norm:
        score += 1.0

    # 拼音子串匹配
    if q_py and q_py in t_py:
        score += 0.9

    # 首字母子串匹配
    if q_ini and q_ini in t_ini:
        score += 0.85

    # 相似度
    score += similarity_ratio(q_norm, t_norm) * 0.8
    score += similarity_ratio(q_py, t_py) * 0.6
    score += similarity_ratio(q_ini, t_ini) * 0.5

    return score


def load_music_files():
    """加载music_data文件夹中的音乐文件"""
    global music_files
    music_files.clear()
    
    if not music_data_path.exists():
        music_data_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"创建音乐数据目录: {music_data_path}")
        return
    
    # 支持的音频格式
    audio_extensions = {'.mp3', '.wav', '.flac', '.m4a', '.ogg', '.aac'}
    
    index = 0
    for file_path in music_data_path.iterdir():
        if file_path.is_file() and file_path.suffix.lower() in audio_extensions:
            # 从文件名提取标题（去掉扩展名）
            title = file_path.stem
            music_files[index] = {
                'title': title,
                'path': str(file_path),
                'filename': file_path.name
            }
            index += 1
    
    logger.info(f"加载了 {len(music_files)} 首音乐文件")

# 初始化时加载音乐文件
load_music_files()
# 加载播放计数
load_play_counts()
load_push_groups()

def convert_audio_to_mp3(input_path: str) -> str:
    """将音频文件转换为MP3格式"""
    try:
        # 创建临时文件
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp_file:
            output_path = tmp_file.name
        
        # 使用ffmpeg转换音频
        cmd = [
            'ffmpeg', '-i', input_path, 
            '-acodec', 'libmp3lame', '-ab', '128k',
            '-y', output_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            logger.info(f"音频转换成功: {input_path} -> {output_path}")
            return output_path
        else:
            logger.error(f"音频转换失败: {result.stderr}")
            return input_path
    except Exception as e:
        logger.error(f"音频转换异常: {str(e)}")
        return input_path

# 歌单命令
hachimi_playlist = on_command("哈基米歌单", aliases={"hachimi_playlist", "歌单"}, priority=5, block=True)

@hachimi_playlist.handle()
async def handle_playlist(bot: Bot, event: GroupMessageEvent):
    """显示哈基米歌单（随机30首）"""
    logger.info(f"收到歌单请求: {event.get_plaintext()}")
    
    if not music_files:
        await hachimi_playlist.finish("❌ 没有找到任何音乐文件，请在music_data文件夹中添加音乐文件。")
    
    # 随机抽取30首
    count = min(30, len(music_files))
    all_items = list(music_files.items())
    random_items = random.sample(all_items, k=count) if count > 0 else []

    playlist_text = "🎵 哈基米歌单（随机30首） 🎵\n"
    for index, music_info in random_items:
        playlist_text += f"{index}: {music_info['title']}\n"
    playlist_text += "\n输入 /哈基米点歌 <序号> 播放"
    
    await hachimi_playlist.finish(playlist_text)

# 点歌命令
hachimi_play = on_command("哈基米点歌",priority=5, block=True)


@hachimi_play.handle()
async def handle_play(bot: Bot, event: GroupMessageEvent):
    """播放指定序号的音乐，并计入排行榜"""
    logger.info(f"收到点歌请求: {event.get_plaintext()}")
    
    match = event.get_plaintext().strip()
    number_match = re.search(r"(\d+)", match)
    
    if not number_match:
        await hachimi_play.finish("❌ 请输入正确的序号格式：哈基米点歌 <序号>")
    
    try:
        index = int(number_match.group(1))
    except ValueError:
        await hachimi_play.finish("❌ 序号必须是数字")
    
    if index not in music_files:
        await hachimi_play.finish(f"❌ 序号 {index} 不存在，请查看歌单获取正确的序号")
    
    music_info = music_files[index]
    title = music_info['title']
    file_path = music_info['path']
    filename = music_info['filename']
    
    # 检查文件是否存在
    if not os.path.exists(file_path):
        await hachimi_play.finish(f"❌ 音乐文件不存在：{title}")
    
    # 计入排行榜（只要点播就+1）
    try:
        play_counts_by_filename[filename] = play_counts_by_filename.get(filename, 0) + 1
        save_play_counts()
    except Exception as e:
        logger.warning(f"计数保存失败：{e}")
    
    # 检查文件大小
    file_size = os.path.getsize(file_path)
    max_size = 10 * 1024 * 1024  # 10MB
    if file_size > max_size:
        await hachimi_play.finish(f"❌ 音乐文件过大（{file_size/1024/1024:.1f}MB），无法发送。请使用较小的音频文件。")
    
    try:
        # 尝试多种方式发送音频文件
        logger.info(f"尝试播放文件: {file_path}")
        logger.info(f"文件大小: {os.path.getsize(file_path)} bytes")
        
        # 方法1：直接使用文件路径
        try:
            audio_msg = MessageSegment.record(file=file_path)
            await hachimi_play.send(f"正在为您播放：\n《{title}》\n●━━━━━━─────── 4:15")
            await hachimi_play.send(audio_msg)
            return
        except Exception as e1:
            logger.warning(f"方法1失败: {str(e1)}")
        
        # 方法2：使用base64编码
        try:
            with open(file_path, "rb") as f:
                audio_data = base64.b64encode(f.read()).decode("utf-8")
            
            logger.info(f"base64编码完成，长度: {len(audio_data)}")
            audio_msg = MessageSegment.record(file=f"base64://{audio_data}")
            
            await hachimi_play.send(f"正在为您播放：\n《{title}》\n●━━━━━━─────── 4:15")
            await hachimi_play.send(audio_msg)
            return
        except Exception as e2:
            logger.warning(f"方法2失败: {str(e2)}")
        
        # 如果都失败了，发送错误信息
        raise Exception(f"所有发送方式都失败了。方法1错误: {str(e1)}, 方法2错误: {str(e2)}")
        
    except Exception as e:
        logger.error(f"播放音乐失败: {str(e)}")
        logger.error(f"错误类型: {type(e).__name__}")
        await hachimi_play.finish(f"❌ 播放失败：{str(e)}")

# 来首哈基米（随机播放一首）
random_hachimi = on_command("来首哈基米", aliases={"随机哈基米", "给我来首哈基米"}, priority=5, block=True)


@random_hachimi.handle()
async def handle_random_hachimi(bot: Bot, event: GroupMessageEvent):
    if not music_files:
        await random_hachimi.finish("❌ 没有找到任何音乐文件")

    # 随机选择一首
    all_items = list(music_files.items())
    idx, music_info = random.choice(all_items)
    title = music_info["title"]
    file_path = music_info["path"]

    if not os.path.exists(file_path):
        await random_hachimi.finish(f"❌ 音乐文件不存在：{title}")

    file_size = os.path.getsize(file_path)
    max_size = 10 * 1024 * 1024
    if file_size > max_size:
        await random_hachimi.finish(f"❌ 音乐文件过大（{file_size/1024/1024:.1f}MB），无法发送。请使用较小的音频文件。")

    try:
        # 方法1：直接路径
        try:
            await random_hachimi.send(f"来首哈基米：\n《{title}》\n●━━━━━━─────── 4:15")
            await random_hachimi.send(MessageSegment.record(file=file_path))
            return
        except Exception as e1:
            logger.warning(f"随机播放方法1失败: {e1}")
        # 方法2：base64
        try:
            with open(file_path, "rb") as f:
                audio_data = base64.b64encode(f.read()).decode("utf-8")
            await random_hachimi.send(f"来首哈基米：\n《{title}》\n●━━━━━━─────── 4:15")
            await random_hachimi.send(MessageSegment.record(file=f"base64://{audio_data}"))
            return
        except Exception as e2:
            logger.warning(f"随机播放方法2失败: {e2}")
        raise Exception("发送失败")
    except Exception as e:
        await random_hachimi.finish(f"❌ 播放失败：{e}")

# 开启/关闭定时推送（仅超管/群主/群管）
enable_push_cmd = on_command(
    "开启哈基米推送",
    aliases={"开启哈基米定时", "哈基米推送开启", "开启推送"},
    permission=SUPERUSER | GROUP_ADMIN | GROUP_OWNER,
    priority=5,
    block=True,
)

disable_push_cmd = on_command(
    "关闭哈基米推送",
    aliases={"关闭哈基米定时", "哈基米推送关闭", "关闭推送"},
    permission=SUPERUSER | GROUP_ADMIN | GROUP_OWNER,
    priority=5,
    block=True,
)


@enable_push_cmd.handle()
async def handle_enable_push(bot: Bot, event: GroupMessageEvent):
    gid = getattr(event, "group_id", None)
    if gid is None:
        await enable_push_cmd.finish("❌ 该命令仅限群聊使用")
    if gid in enabled_push_groups:
        await enable_push_cmd.finish("ℹ️ 本群已开启定时推送")
    enabled_push_groups.add(int(gid))
    save_push_groups()
    msg = "✅ 已开启本群哈基米定时推送：每天 8:00、12:00、18:00、21:00 推送一首哈基米音乐"
    if not _SCHEDULER_AVAILABLE:
        msg += "\n⚠️ 未检测到 nonebot_plugin_apscheduler，定时任务将无法执行。请安装并启用该插件。"
    await enable_push_cmd.finish(msg)


@disable_push_cmd.handle()
async def handle_disable_push(bot: Bot, event: GroupMessageEvent):
    gid = getattr(event, "group_id", None)
    if gid is None:
        await disable_push_cmd.finish("❌ 该命令仅限群聊使用")
    if int(gid) not in enabled_push_groups:
        await disable_push_cmd.finish("ℹ️ 本群未开启定时推送")
    enabled_push_groups.discard(int(gid))
    save_push_groups()
    await disable_push_cmd.finish("✅ 已关闭本群哈基米定时推送")


async def _push_one_group(bot: Bot, group_id: int, title: str, file_path: str, prefix: str) -> None:
    try:
        # 先发文本
        await bot.send_group_msg(group_id=group_id, message=f"{prefix}\n《{title}》\n●━━━━━━─────── 4:15")
        # 尝试音频
        try:
            await bot.send_group_msg(group_id=group_id, message=MessageSegment.record(file=file_path))
            return
        except Exception as e1:
            logger.warning(f"群{group_id}推送方法1失败: {e1}")
        try:
            with open(file_path, "rb") as f:
                audio_data = base64.b64encode(f.read()).decode("utf-8")
            await bot.send_group_msg(group_id=group_id, message=MessageSegment.record(file=f"base64://{audio_data}"))
        except Exception as e2:
            logger.error(f"群{group_id}推送方法2失败: {e2}")
    except Exception as e:
        logger.error(f"推送到群{group_id}失败: {e}")


async def _push_random_to_enabled_groups(prefix: str) -> None:
    if not music_files or not enabled_push_groups:
        return
    # 随机挑一首
    idx, info = random.choice(list(music_files.items()))
    title = info["title"]
    file_path = info["path"]
    if not os.path.exists(file_path):
        return
    # 大小限制
    max_size = 10 * 1024 * 1024
    if os.path.getsize(file_path) > max_size:
        return

    for bot_id, bot in get_bots().items():
        for gid in list(enabled_push_groups):
            await _push_one_group(bot, gid, title, file_path, prefix)


def _register_cron_jobs() -> None:
    if not _SCHEDULER_AVAILABLE:
        logger.warning("未检测到 nonebot_plugin_apscheduler，哈基米定时推送将不可用")
        return

    # 避免重复注册
    exist_ids = {job.id for job in scheduler.get_jobs()}
    jobs = [
        ("hachimi_push_0800", 8, 0, "早上好，早饭时间，来首哈基米音乐！"),
        ("hachimi_push_1200", 12, 0, "中午好，午饭时间，来首哈基米音乐！"),
        ("hachimi_push_1800", 18, 0, "傍晚好，晚饭时间，来首哈基米音乐！"),
        ("hachimi_push_2100", 21, 0, "晚上好，该睡觉了，来首哈基米音乐！"),
    ]

    for job_id, hour, minute, prefix in jobs:
        if job_id in exist_ids:
            continue

        async def job_runner(p=prefix):
            try:
                await _push_random_to_enabled_groups(p)
            except Exception as e:
                logger.error(f"哈基米定时推送执行失败：{e}")

        scheduler.add_job(job_runner, "cron", hour=hour, minute=minute, id=job_id, replace_existing=True)


# 注册定时任务
_register_cron_jobs()

# 重新加载音乐文件命令（管理员功能）
reload_music = on_command("重载哈基米歌单", aliases={"reload_hachimi"}, priority=5, block=True)

@reload_music.handle()
async def handle_reload(bot: Bot, event: GroupMessageEvent):
    """重新加载音乐文件"""
    logger.info("重新加载音乐文件")
    load_music_files()
    count = len(music_files)
    await reload_music.finish(f"✅ 已重新加载音乐文件，共找到 {count} 首音乐")

# 排行榜命令（分页，每页30）
hachimi_rank = on_command("哈基米排行榜", aliases={"hachimi_rank", "排行榜"}, priority=5, block=True)

@hachimi_rank.handle()
async def handle_rank(bot: Bot, event: GroupMessageEvent):
    text = event.get_plaintext().strip()
    # 提取可选页码
    m = re.search(r"(\d+)$", text)
    page = 1
    if m:
        try:
            page = max(1, int(m.group(1)))
        except Exception:
            page = 1
    page_size = 30
    offset = (page - 1) * page_size

    if not music_files:
        await hachimi_rank.finish("❌ 没有找到任何音乐文件")

    # 根据文件名计数构建排名（只统计被播放过的）
    filename_to_index = {info['filename']: idx for idx, info in music_files.items()}

    played_items = [
        (fn, cnt) for fn, cnt in play_counts_by_filename.items() if cnt > 0 and fn in filename_to_index
    ]
    # 按次数降序，次数相同则按文件名升序
    played_items.sort(key=lambda x: (-x[1], x[0]))

    # 获取本页被播放过的条目
    page_items: List[Tuple[str, int]] = played_items[offset: offset + page_size]

    # 若不足30条，用未上榜或剩余歌曲随机补齐
    needed = page_size - len(page_items)
    if needed > 0:
        # 取所有可用文件名中未在本页的
        already_fns = {fn for fn, _ in page_items}
        # 为了避免越界，先取所有歌曲文件名
        all_fns = [info['filename'] for _, info in music_files.items()]
        candidates = [fn for fn in all_fns if fn not in already_fns]
        if candidates:
            supplement = random.sample(candidates, k=min(needed, len(candidates)))
            page_items.extend((fn, play_counts_by_filename.get(fn, 0)) for fn in supplement)

    if not page_items:
        await hachimi_rank.finish("❌ 暂无排行榜数据")

    # 组装显示（映射为当前序号）
    rank_text = f"🏆 哈基米排行榜 第{page}页（每页30）\n"
    for i, (fn, cnt) in enumerate(page_items, start=1 + offset):
        idx = filename_to_index.get(fn, None)
        title = music_files[idx]['title'] if idx in music_files else fn
        rank_text += f"{i}. {title}（序号{idx if idx is not None else '-'}） - {cnt} 次\n"

    rank_text += "\n输入 /哈基米点歌 <序号> 播放"
    await hachimi_rank.finish(rank_text)

# 搜索命令（支持ID、模糊、正则、拼音、首字母；最多10条）
hachimi_search = on_command("哈基米搜索", aliases={"hachimi_search", "搜索"}, priority=5, block=True)

@hachimi_search.handle()
async def handle_search(bot: Bot, event: GroupMessageEvent):
    text = event.get_plaintext().strip()
    # 去掉命令本身
    # 兼容：命令后直接空格关键词
    query = re.sub(r"^\s*/?\s*(哈基米搜索|搜索|hachimi_search)\s*", "", text)
    query = query.strip()

    if not query:
        await hachimi_search.finish("❌ 用法：/哈基米搜索 <ID或关键词或正则>")

    # 1) 纯数字直接按ID匹配
    if query.isdigit():
        idx = int(query)
        if idx in music_files:
            info = music_files[idx]
            await hachimi_search.finish(f"找到ID对应曲目：\n{idx}: {info['title']}\n输入 /哈基米点歌 {idx} 播放")
        # 若ID不存在，则继续模糊搜索

    # 2) 计算分数
    scored: List[tuple[int, float]] = []
    for idx, info in music_files.items():
        title = info['title']
        score = compute_match_score(query, title)
        scored.append((idx, score))

    # 排序并截取前10
    scored.sort(key=lambda x: x[1], reverse=True)

    # 过滤无关项（给个最低阈值），若全部低于阈值也给前10防止为空
    threshold = 0.25
    top = [item for item in scored if item[1] >= threshold][:10]
    if not top:
        top = scored[:10]

    if not top:
        await hachimi_search.finish("❌ 未找到相关曲目")

    result_text = "🔎 搜索结果（最多10条）：\n"
    for idx, score in top:
        info = music_files[idx]
        result_text += f"{idx}: {info['title']}\n"
    result_text += "\n输入 /哈基米点歌 <序号> 播放"

    # 若pypinyin不可用，提示一次
    if not _PYPINYIN_AVAILABLE:
        result_text += "\n⚠️ 建议安装 pypinyin 以强化拼音与首字母匹配：pip install pypinyin"

    await hachimi_search.finish(result_text)

# 哈基米帮助
hachimi_help = on_command("哈基米帮助", aliases={"hachimi_help", "哈基米菜单", "哈基米指令", "帮助"}, priority=5, block=True)

@hachimi_help.handle()
async def handle_help(bot: Bot, event: GroupMessageEvent):
    lines: List[str] = [
        "📖 哈基米帮助/指令列表",
        "/哈基米帮助 - 查看帮助/指令列表",
        "/哈基米歌单 - 随机展示30首可播放的音乐",
        "/哈基米点歌 <序号> - 播放指定序号的音乐（并计入排行榜）",
        "/来首哈基米 - 随机播放一首音乐（别名：随机哈基米、给我来首哈基米）",
        "/哈基米搜索 <关键词或正则或ID> - 搜索乐曲（最多返回10条）",
        "/哈基米排行榜 [页码] - 查看排行榜（每页30条，默认第1页）",
        "/开启哈基米推送 - 开启每日定时推送（管理员）",
        "/关闭哈基米推送 - 关闭每日定时推送（管理员）",
        ""]
    await hachimi_help.finish("\n".join(lines))

# 添加一个简单的测试命令
test_cmd = on_command("测试哈基米", priority=5, block=True)

@test_cmd.handle()
async def handle_test(bot: Bot, event: GroupMessageEvent):
    """测试插件是否正常工作"""
    logger.info("收到测试命令")
    await test_cmd.finish("✅ 哈基米音乐插件正常工作！")

