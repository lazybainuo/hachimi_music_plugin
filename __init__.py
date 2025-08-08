import os
import re
from pathlib import Path
from typing import List, Dict, Optional

from nonebot import on_command, on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me

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

# 初始化时加载音乐文件
load_music_files()

# 歌单命令
hachimi_playlist = on_command("哈基米歌单", aliases={"hachimi_playlist"}, priority=5, block=True)

@hachimi_playlist.handle()
async def handle_playlist(bot: Bot, event: GroupMessageEvent):
    """显示哈基米歌单"""
    if not music_files:
        await hachimi_playlist.finish("❌ 没有找到任何音乐文件，请在music_data文件夹中添加音乐文件。")
    
    playlist_text = "🎵 哈基米歌单 🎵\n"
    for index, music_info in music_files.items():
        playlist_text += f"{index}: {music_info['title']}\n"
    playlist_text += "\n输入 /哈基米点歌 <序号> 播放"
    
    await hachimi_playlist.finish(playlist_text)

# 点歌命令
hachimi_play = on_regex(r"^/哈基米点歌\s*(\d+)$", priority=5, block=True)

@hachimi_play.handle()
async def handle_play(bot: Bot, event: GroupMessageEvent):
    """播放指定序号的音乐"""
    match = event.get_plaintext().strip()
    number_match = re.search(r"(\d+)", match)
    
    if not number_match:
        await hachimi_play.finish("❌ 请输入正确的序号格式：/哈基米点歌 <序号>")
    
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
    
    try:
        # 发送音乐文件
        audio_msg = MessageSegment.record(file=file_path)
        await hachimi_play.send(f"正在为您播放：\n《{title}》\n●━━━━━━─────── 4:15")
        await hachimi_play.send(audio_msg)
    except Exception as e:
        await hachimi_play.finish(f"❌ 播放失败：{str(e)}")

# 重新加载音乐文件命令（管理员功能）
reload_music = on_command("重载哈基米歌单", aliases={"reload_hachimi"}, priority=5, block=True)

@reload_music.handle()
async def handle_reload(bot: Bot, event: GroupMessageEvent):
    """重新加载音乐文件"""
    load_music_files()
    count = len(music_files)
    await reload_music.finish(f"✅ 已重新加载音乐文件，共找到 {count} 首音乐")

