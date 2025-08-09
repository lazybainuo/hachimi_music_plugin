import os
import re
import base64
import subprocess
import tempfile
from pathlib import Path
from typing import List, Dict, Optional

from nonebot import on_command, on_regex, logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="hachimi_music_plugin",
    description="哈基米音乐播放插件",
    usage="""
    命令列表：
    /哈基米歌单 - 显示所有可播放的音乐列表
    /哈基米点歌 <序号> - 播放指定序号的音乐
    """,
    config=Config,
)

# 音乐文件映射
music_files: Dict[int, Dict[str, str]] = {}
music_data_path = Path(__file__).parent / "music_data"

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
    """显示哈基米歌单"""
    logger.info(f"收到歌单请求: {event.get_plaintext()}")
    
    if not music_files:
        await hachimi_playlist.finish("❌ 没有找到任何音乐文件，请在music_data文件夹中添加音乐文件。")
    
    playlist_text = "🎵 哈基米歌单 🎵\n"
    for index, music_info in music_files.items():
        playlist_text += f"{index}: {music_info['title']}\n"
    playlist_text += "\n输入 /哈基米点歌 <序号> 播放"
    
    await hachimi_playlist.finish(playlist_text)

# 点歌命令
hachimi_play = on_command("哈基米点歌",priority=5, block=True)


@hachimi_play.handle()
async def handle_play(bot: Bot, event: GroupMessageEvent):
    """播放指定序号的音乐"""
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
    
    # 检查文件是否存在
    if not os.path.exists(file_path):
        await hachimi_play.finish(f"❌ 音乐文件不存在：{title}")
    
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

# 重新加载音乐文件命令（管理员功能）
reload_music = on_command("重载哈基米歌单", aliases={"reload_hachimi"}, priority=5, block=True)

@reload_music.handle()
async def handle_reload(bot: Bot, event: GroupMessageEvent):
    """重新加载音乐文件"""
    logger.info("重新加载音乐文件")
    load_music_files()
    count = len(music_files)
    await reload_music.finish(f"✅ 已重新加载音乐文件，共找到 {count} 首音乐")

# 添加一个简单的测试命令
test_cmd = on_command("测试哈基米", priority=5, block=True)

@test_cmd.handle()
async def handle_test(bot: Bot, event: GroupMessageEvent):
    """测试插件是否正常工作"""
    logger.info("收到测试命令")
    await test_cmd.finish("✅ 哈基米音乐插件正常工作！")

