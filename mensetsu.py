import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone, time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
import uuid
from collections import defaultdict

# ------------------------------------------------
# dotenv 読み込み・TOKEN設定
# ------------------------------------------------
load_dotenv()
BOT_TOKEN: Optional[str] = os.getenv("DISCORD_BOT_TOKEN")
if not BOT_TOKEN:
    BOT_TOKEN = "YOUR_TOKEN_HERE"

# ------------------------------------------------
# グローバル変数定義
# ------------------------------------------------
import os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, 'monthly_counts_data.json')
DATA_FILE_PATH = os.path.join(BASE_DIR, 'interview_records.json')
BAN_DATA_FILE = os.path.join(BASE_DIR, 'ban_data.json')
LOG_CHANNEL_ID: int = 1306053871855996979
# ★ 追記: 自動キックした際のログ出力先チャンネル
AUTO_KICK_LOG_CHANNEL_ID: int = 1361465393587163166

MONTHLY_GOAL: int = 10
transient_memo_cache: Dict[str, str] = {}

# ------------------------------------------------
# 設定 (config)
# ------------------------------------------------
# 【コード②：面接／進捗管理用】
MAIN_GUILD_ID: int = 784723518402592799
MAIN_CATEGORY_ID: int = 1305735985539055667
DASHBOARD_CHANNEL_ID: int = 1305732338499457075
INTERVIEWER_ROLE_ID: int = 892528673620623411  # 面接担当者ロールID
PROFILE_FORM_CHANNEL_ID: int = 1305911809576013926
SPECIFIC_ROLE_ID: int = 784723518402592803
PASS_ROLE_ID: int = 1304670207238996052
OTHER_SERVER_PASS_ROLE_NAME: str = "合格者"  # サブサーバー用の合格ロール名
INTERVIEW_MEMO_CHANNEL_ID: int = 1029739734945235014  # 面接メモ用（合否ボタン付）
PASS_MEMO_CHANNEL_ID: int = 1305384589111595088
ADDITIONAL_MEMO_CHANNEL_ID: int = 872102367636643870  # 追加メモ用（ボタンなし）
INTERVIEWER_REMIND_CHANNEL_ID: int = 1306090666891411476
INTERVIEWER_STATS_CHANNEL_ID: int = 1306053871855996979  # 統計表示先（担当者別）
MONTHLY_STATS_CHANNEL_ID: int = 1313069444272099449  # 統計表示先（月ごと合否）
ADMIN_ROLE_ID = 991112832655560825  # 管理者ロールID（手動追加用）
DATA_FILE_PATH = os.path.join(BASE_DIR, 'interview_records.json')

# JSTタイムゾーン
JST: timezone = timezone(timedelta(hours=9))

# ------------------------------------------------
# ログ設定
# ------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(name)s: %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger: logging.Logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# 進捗バー付きで統計 Embed を生成・既存メッセージを編集する
# ─────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────
# 月次進捗バー表示 ― 既存メッセージを探して更新
# ─────────────────────────────────────────────────────────
async def update_stats(bot: commands.Bot) -> None:
    """面接回数バーを生成し、既存の月次 Embed を編集する。"""
    now        = datetime.now(JST)
    year_month = f"{now.year}-{now.month:02d}"

    # === 集計 ===============================================================
    from collections import defaultdict
    exec_counts: defaultdict[int, int] = defaultdict(int)
    for rec in data_manager.interview_records:
        try:
            dt = datetime.fromisoformat(rec.get("date"))
        except Exception:
            continue
        if dt.year == now.year and dt.month == now.month:
            exec_counts[rec.get("interviewer_id")] += 1

    # === 並び替え (回数↓→名前↑) ============================================
    guild_main = bot.get_guild(MAIN_GUILD_ID)
    sorted_exec = sorted(
        exec_counts.items(),
        key=lambda x: (-x[1], guild_main.get_member(x[0]).display_name if guild_main else "")
    )

    # === Embed 作成 ==========================================================
    embed = discord.Embed(
        title       = f"面接担当者の面接回数 {year_month}",
        description = "各担当者の面接回数と進捗を以下に表示します。\n目標は **10回** です。",
        color       = 0xf1c40f,
        timestamp   = datetime.utcnow()
    )
    if sorted_exec:
        for uid, cnt in sorted_exec:
            member = guild_main.get_member(uid) if guild_main else None
            name   = member.display_name if member else f"ID:{uid}"
            bar    = generate_custom_progress_bar(cnt, MONTHLY_GOAL)
            embed.add_field(name=name, value=bar, inline=False)
    else:
        embed.add_field(name="データなし", value="今月の記録がまだありません。", inline=False)

    # === 送信先チャンネル ====================================================
    channel = bot.get_channel(INTERVIEWER_STATS_CHANNEL_ID)
    if not channel or not isinstance(channel, discord.TextChannel):
        logger.warning("update_stats: 統計出力チャンネルが見つかりませんでした。")
        return

    # === 既存メッセージ ID を取得（キーは year_month） ======================
    msg_id = data_manager.interviewer_stats_message_ids.get(year_month)  # 新ロジック
    if not msg_id:                                                       # 旧キーとの互換
        msg_id = data_manager.interviewer_stats_message_ids.get("current")

    target_msg = None

    # 1) ID があれば fetch
    if msg_id:
        try:
            target_msg = await channel.fetch_message(msg_id)
        except discord.NotFound:
            target_msg = None

    # 2) ID が無い / 失敗した場合 → 履歴をスキャンして探す
    if target_msg is None:
        async for hist_msg in channel.history(limit=50):
            if hist_msg.author.id != bot.user.id or not hist_msg.embeds:
                continue
            title = hist_msg.embeds[0].title or ""
            if title.startswith(f"面接担当者の面接回数 {year_month}"):
                target_msg = hist_msg
                break

    # 3) 編集 or 新規送信
    try:
        if target_msg:
            await target_msg.edit(embed=embed)
            saved_id = target_msg.id
        else:
            sent_msg = await channel.send(embed=embed)
            saved_id = sent_msg.id
    except Exception as e:
        logger.error(f"update_stats: メッセージ編集／送信失敗: {e}")
        return

    # === ID を保存（year_month キーで保存し、旧 'current' も維持） ==========
    data_manager.interviewer_stats_message_ids[year_month] = saved_id
    data_manager.interviewer_stats_message_ids["current"]  = saved_id   # 後方互換
    await data_manager.save_data()
    logger.info("update_stats: 既存メッセージを更新しました。")
    await update_monthly_stats(bot)

async def update_monthly_stats(bot: commands.Bot) -> None:
    """
    当月の『面接結果内訳』を

        合格者数:
        不合格者数:
        合格率:

    だけでまとめて、既存チャンネル 1313069444272099449 の
    既存 Embed を上書き更新する。
    """
    # ---------------- 対象年月 ----------------
    now        = datetime.now(JST)
    year_month = f"{now.year}-{now.month:02d}"

    # ---------------- 集計 -------------------
    pass_cnt = 0
    fail_cnt = 0   # 不合格/BAN/インターバル + 各遅延

    for rec in data_manager.interview_records:
        try:
            dt = datetime.fromisoformat(rec.get("date"))
        except Exception:
            continue
        if dt.year != now.year or dt.month != now.month:
            continue

        result = str(rec.get("result", "")).upper()
        if result == "PASS":
            pass_cnt += 1
        elif result.startswith(("FAIL", "BAN", "INTERVAL")):
            fail_cnt += 1

    total     = pass_cnt + fail_cnt
    pass_rate = (pass_cnt / total) * 100 if total else 0.0

    # ---------------- Embed ------------------
    embed = discord.Embed(
        title     = f"面接結果内訳 {year_month}",
        color     = 0x3498db,
        timestamp = datetime.utcnow()
    )
    embed.add_field(name="合格者数",   value=f"{pass_cnt} 人",   inline=False)
    embed.add_field(name="不合格者数", value=f"{fail_cnt} 人",   inline=False)
    embed.add_field(name="合格率",     value=f"{pass_rate:.1f}% ", inline=False)

    # ---------------- 送信 / 更新 -------------
    channel: Optional[discord.TextChannel] = bot.get_channel(MONTHLY_STATS_CHANNEL_ID)
    if channel is None:
        logger.warning("update_monthly_stats: 出力チャンネルが見つかりません。")
        return

    msg_id = data_manager.monthly_stats_message_ids.get(year_month)
    target_msg: Optional[discord.Message] = None

    if msg_id:
        try:
            target_msg = await channel.fetch_message(msg_id)
        except discord.NotFound:
            target_msg = None

    if target_msg is None:      # タイトル一致で履歴検索（後方互換）
        async for hist in channel.history(limit=50):
            if hist.author.id == bot.user.id and hist.embeds:
                t = hist.embeds[0].title or ""
                if t.startswith(f"面接結果内訳 {year_month}"):
                    target_msg = hist
                    break

    try:
        if target_msg:
            await target_msg.edit(embed=embed)
            saved_id = target_msg.id
        else:
            sent = await channel.send(embed=embed)
            saved_id = sent.id
    except Exception as e:
        logger.error(f"update_monthly_stats: メッセージ送信/編集に失敗: {e}")
        return

    data_manager.monthly_stats_message_ids[year_month] = saved_id
    await data_manager.save_data()
    logger.info("update_monthly_stats: 月次内訳を更新しました。")

# ------------------------------------------------
# ★ 追記: 自動キック時に特定チャンネルへログ出力する関数
# ------------------------------------------------
async def log_auto_kick(bot: commands.Bot, user: discord.abc.User, guild: discord.Guild, reason: str):
    """
    自動キックが発生した際に、指定のチャンネル(AUTO_KICK_LOG_CHANNEL_ID)へ
    ユーザー/サーバー/理由を含むログを送信するヘルパー関数
    """
    channel = bot.get_channel(AUTO_KICK_LOG_CHANNEL_ID)
    if channel and isinstance(channel, discord.TextChannel):
        await channel.send(
            f"【自動キック】\n"
            f"ユーザー: {user.mention} (ID: {user.id})\n"
            f"サーバー: {guild.name}\n"
            f"理由: {reason}"
        )
    else:
        logger.warning("自動キックのログ出力先チャンネルが見つかりませんでした。")


# ------------------------------------------------
# BANデータ管理用クラス
# ------------------------------------------------
class BanManager:
    def __init__(self, file_path: str = "ban_data.json"):
        self.file_path = file_path
        self.ban_records = {}  # {"ユーザーID": {"ban_origin": "main" or "sub", "ban_type": "BAN" or "INTERVAL", "ban_time": ISO文字列}}
        self.load_data()

    def load_data(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    self.ban_records = json.load(f)
                logger.info("Banデータロード成功")
            except Exception as e:
                logger.error(f"Banデータロード失敗: {e}")
        else:
            logger.info("Banデータファイルなし。新規作成します。")

    def save_data(self):
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.ban_records, f, ensure_ascii=False, indent=4)
            logger.info("Banデータ保存成功")
        except Exception as e:
            logger.error(f"Banデータ保存失敗: {e}")

    def add_ban(self, user_id: int, ban_origin: str, ban_type: str):
        self.ban_records[str(user_id)] = {
            "ban_origin": ban_origin,
            "ban_type": ban_type,
            "ban_time": get_current_time_iso()
        }
        self.save_data()

    def remove_ban(self, user_id: int):
        if str(user_id) in self.ban_records:
            del self.ban_records[str(user_id)]
            self.save_data()

    def check_ban(self, user_id: int):
        record = self.ban_records.get(str(user_id))
        if record:
            if record["ban_type"] == "INTERVAL":
                ban_time = datetime.fromisoformat(record["ban_time"])
                if datetime.now(JST) - ban_time >= timedelta(days=90):
                    self.remove_ban(user_id)
                    return None
            return record
        return None

    def remove_expired(self):
        to_remove = []
        for user_id, record in self.ban_records.items():
            if record["ban_type"] == "INTERVAL":
                ban_time = datetime.fromisoformat(record["ban_time"])
                if datetime.now(JST) - ban_time >= timedelta(days=90):
                    to_remove.append(user_id)
        for user_id in to_remove:
            del self.ban_records[user_id]
        if to_remove:
            self.save_data()


ban_manager = BanManager(BAN_DATA_FILE)

# ------------------------------------------------
# ダッシュボード更新のデバウンス処理
# ------------------------------------------------
_dashboard_update_lock = asyncio.Lock()
_dashboard_update_task: Optional[asyncio.Task] = None


def request_dashboard_update(bot: discord.Client, delay: float = 2.0):
    async def update_after_delay():
        try:
            await asyncio.sleep(delay)
            await update_dashboard(bot)
        except asyncio.CancelledError:
            pass
        finally:
            global _dashboard_update_task
            _dashboard_update_task = None

    async def wrapper():
        global _dashboard_update_task
        async with _dashboard_update_lock:
            if _dashboard_update_task is not None:
                _dashboard_update_task.cancel()
            _dashboard_update_task = asyncio.create_task(update_after_delay())

    # 同期関数内でバックグラウンドタスクとして wrapper() をスケジュールする
    asyncio.create_task(wrapper())

# ------------------------------------------------
# 共通ユーティリティ関数
# ------------------------------------------------
def get_current_time_iso() -> str:
    return datetime.now(JST).isoformat()


def make_progress_key(guild_id: int, member_id: int) -> str:
    return f"{guild_id}-{member_id}"


async def update_or_send_message(
        channel: discord.TextChannel,
        current_msg_id: Optional[int],
        content: str,
        embed: Optional[discord.Embed] = None
) -> int:
    try:
        if current_msg_id:
            msg = await channel.fetch_message(current_msg_id)
            if msg.author.id == channel.guild.me.id:
                await msg.edit(content=content, embed=embed)
                return msg.id
        new_msg = await channel.send(content=content, embed=embed)
        return new_msg.id
    except discord.NotFound:
        new_msg = await channel.send(content=content, embed=embed)
        return new_msg.id


async def delete_candidate_channels(guild: discord.Guild, candidate_id: int) -> None:
    progress_key = make_progress_key(guild.id, candidate_id)
    text_channel: Optional[discord.TextChannel] = None
    for ch in guild.text_channels:
        if ch.id in data_manager.interview_channel_mapping and data_manager.interview_channel_mapping[ch.id] == progress_key:
            text_channel = ch
            break
    if text_channel:
        try:
            await text_channel.delete()
            logger.info(f"候補者 {candidate_id} のテキストチャンネル {text_channel.id} を削除")
        except Exception as e:
            logger.error(f"テキストチャンネル {text_channel.id} 削除失敗: {e}")
        data_manager.interview_channel_mapping.pop(text_channel.id, None)
    cp: Dict[str, Any] = data_manager.candidate_progress.get(progress_key, {})
    vc_channel_id: Optional[int] = cp.get('voice_channel_id')
    if vc_channel_id:
        vc: Optional[discord.VoiceChannel] = guild.get_channel(vc_channel_id)
        if vc:
            try:
                await vc.delete()
                logger.info(f"候補者 {candidate_id} のボイスチャンネル {vc_channel_id} を削除")
            except Exception as e:
                logger.error(f"ボイスチャンネル {vc_channel_id} 削除失敗: {e}")
        cp.pop('voice_channel_id', None)


def update_candidate_status(cp: Dict[str, Any], status: str) -> None:
    cp['status'] = status
    cp['timestamp'] = get_current_time_iso()


async def ensure_channel_exists(
        bot: discord.Client, progress_key: str, cp: Dict[str, Any]
) -> Optional[discord.TextChannel]:
    channel = bot.get_channel(cp.get('channel_id'))
    if not channel:
        data_manager.candidate_progress.pop(progress_key, None)
        await data_manager.save_data()
        await update_dashboard(bot)
        return None
    return channel


def generate_custom_progress_bar(count: int, goal: int) -> str:
    if count <= goal:
        filled = "🟩" * count
        empty = "⬜" * (goal - count)
        bar = filled + empty
        return f"[{bar}] {count}/{goal}回"
    else:
        extra = count - goal
        if count < 20:
            colored_block = "🟨"
        elif count < 30:
            colored_block = "🟧"
        else:
            colored_block = "🟥"
        base_bar = colored_block * goal
        extra_bar = "★" * extra
        return f"[{base_bar}{extra_bar}] {count}/{goal}回 (+{extra}超過)"

class utils:
    @staticmethod
    async def safe_fetch_member(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
        except discord.HTTPException:
            logger.warning(f"fetch_member failed in guild {guild.id}")
            return None


# ------------------------------------------------
# DataManager（永続化用）
# ------------------------------------------------
class DataManager:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.lock = asyncio.Lock()
        self.interview_records: List[Dict[str, Any]] = []
        self.interviewer_stats_message_ids: Dict[str, int] = {}
        self.monthly_stats_message_ids: Dict[str, int] = {}
        self.candidate_progress: Dict[str, Dict[str, Any]] = {}
        self.interview_channel_mapping: Dict[int, str] = {}
        self.dashboard_message_id: Optional[int] = None
        self.memo_history: Dict[str, List[Dict[str, Any]]] = {}
        self.load_data()

    async def save_data(self) -> None:
        async with self.lock:
            data: Dict[str, Any] = {
                'interview_records': self.interview_records,
                'interviewer_stats_message_ids': self.interviewer_stats_message_ids,
                'monthly_stats_message_ids': self.monthly_stats_message_ids,
                'candidate_progress': self.candidate_progress,
                'interview_channel_mapping': {str(k): v for k, v in self.interview_channel_mapping.items()},
                'dashboard_message_id': self.dashboard_message_id,
                'memo_history': self.memo_history
            }
            try:
                with open(self.file_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
                logger.info("データ保存に成功")
            except Exception as e:
                logger.error(f"データ保存に失敗: {e}")

    def load_data(self) -> None:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 両対応：リスト形式 or 辞書形式
                    if isinstance(data, list):
                        self.interview_records = data
                        self.interviewer_stats_message_ids = {}
                        self.monthly_stats_message_ids = {}
                        self.candidate_progress = {}
                        self.interview_channel_mapping = {}
                        self.dashboard_message_id = None
                        self.memo_history = {}
                    elif isinstance(data, dict):
                        self.interview_records = data.get('interview_records', [])
                        self.interviewer_stats_message_ids = data.get('interviewer_stats_message_ids', {})
                        self.monthly_stats_message_ids = data.get('monthly_stats_message_ids', {})
                        self.candidate_progress = data.get('candidate_progress', {})
                        imap = data.get('interview_channel_mapping', {})
                        self.interview_channel_mapping = {int(k): v for k, v in imap.items()}
                        self.dashboard_message_id = data.get('dashboard_message_id')
                        self.memo_history = data.get('memo_history', {})
                    else:
                        self.interview_records = []
                        self.interviewer_stats_message_ids = {}
                        self.monthly_stats_message_ids = {}
                        self.candidate_progress = {}
                        self.interview_channel_mapping = {}
                        self.dashboard_message_id = None
                        self.memo_history = {}
                logger.info("データロードに成功")
            except Exception as e:
                import traceback
                logger.error(f"データロードに失敗: {e}\n{traceback.format_exc()}")
        else:
            logger.warning(f"データファイルなし。空の状態から開始します。({self.file_path} が見つかりません。カレントディレクトリ: {os.getcwd()})")

data_manager = DataManager(DATA_FILE_PATH)


# ------------------------------------------------
# 通知／補助関数
# ------------------------------------------------
async def send_interviewer_notification(bot: discord.Client, remind_channel: discord.TextChannel,
                                        candidate_channel: discord.TextChannel) -> None:
    await remind_channel.send(
        f"日程調整お願いします。 <@&{INTERVIEWER_ROLE_ID}> {candidate_channel.mention}"
    )


def get_interviewer_role(guild: discord.Guild) -> Optional[discord.Role]:
    if guild.id == MAIN_GUILD_ID:
        role: Optional[discord.Role] = guild.get_role(INTERVIEWER_ROLE_ID)
        if role is None:
            logger.warning(f"ID {INTERVIEWER_ROLE_ID} の面接担当者ロールが見つかりません")
        return role
    else:
        role = discord.utils.get(guild.roles, name="面接手伝い")
        if role is None:
            logger.warning(f"名前 '面接手伝い' のロールが見つかりません")
        return role


async def update_dashboard(bot: discord.Client) -> None:
    dashboard_channel: Optional[discord.TextChannel] = bot.get_channel(DASHBOARD_CHANNEL_ID)
    if dashboard_channel is None:
        logger.error(f"ダッシュボードチャンネル (ID: {DASHBOARD_CHANNEL_ID}) が見つかりません")
        return
    now = datetime.now(JST)
    status_mapping = {
        "プロフィール未記入": "プロフィール未記入",
        "記入済み": "記入済み",
        "担当者待ち": "担当者待ち",
        "日程調整済み": "日程調整済み",
        "面接済み": "面接済み",
        "不合格": None,
    }
    embed_config = {
        "プロフィール未記入": {"title": "⚠️ プロフィール未記入", "color": 0x808080},
        "記入済み": {"title": "要連絡！", "color": 0x00FF00},
        "担当者待ち": {"title": "日程調整してね！", "color": 0xFF0000},
        "日程調整済み": {"title": "📅 日程調整済み", "color": 0x0000FF},
        "面接済み": {"title": "✅ 面接済み", "color": 0x808080},
    }
    dashboard_sections = {key: [] for key in embed_config.keys()}

    for progress_key, cp in data_manager.candidate_progress.items():
        orig_status = cp.get("status", "")
        display_key = status_mapping.get(orig_status)
        if display_key is None:
            continue
        channel_obj: Optional[discord.TextChannel] = bot.get_channel(cp.get("channel_id"))
        if channel_obj is None:
            logger.warning(f"候補者 {progress_key} のチャンネル (ID: {cp.get('channel_id')}) が見つかりません")
            continue
        channel_link: str = channel_obj.mention
        if orig_status != "日程調整済み" and cp.get("voice_channel_id"):
            vc_obj: Optional[discord.VoiceChannel] = bot.get_channel(cp.get("voice_channel_id"))
            if vc_obj:
                channel_link += f" (VC: {vc_obj.mention})"
        candidate_id = cp.get("candidate_id")
        candidate: Optional[discord.User] = bot.get_user(candidate_id)
        if not candidate:
            logger.warning(f"候補者 {candidate_id} が見つかりません")
            continue

        entry = ""
        if orig_status in ["担当者待ち", "日程調整済み", "面接済み"]:
            interviewer = bot.get_user(cp.get("interviewer_id")) if cp.get("interviewer_id") else None
            interviewer_name = interviewer.display_name if interviewer else "未設定"
            interview_time_str = ""
            if cp.get("interview_time"):
                try:
                    it = datetime.fromisoformat(cp.get("interview_time"))
                    interview_time_str = it.strftime('%m/%d %H:%M')
                except Exception as e:
                    logger.error(f"面接時間解析失敗: {e}")
            entry = f"**{interviewer_name}** | {interview_time_str} | {candidate.display_name} {channel_link}"
        elif orig_status == "記入済み":
            prefix = ""
            if cp.get("profile_filled_time"):
                try:
                    filled_time = datetime.fromisoformat(cp.get("profile_filled_time"))
                    hours_passed = (now - filled_time).total_seconds() / 3600
                    if hours_passed >= 16:
                        prefix = ":bangbang: "
                    elif hours_passed >= 8:
                        prefix = ":exclamation: "
                except Exception as e:
                    logger.error(f"記入済み時間解析失敗: {e}")
            entry = f"{prefix}**{candidate.display_name}** {channel_link}"
        else:
            entry = f"{candidate.display_name} {channel_link}"
        dashboard_sections[display_key].append(entry)

    dashboard_sections["記入済み"].sort()
    for key in ["担当者待ち", "面接済み"]:
        dashboard_sections[key].sort(key=lambda entry: entry.split(" | ")[0])
    if dashboard_sections["日程調整済み"]:
        def sort_key(entry: str):
            parts = entry.split(" | ")
            interviewer_name = parts[0].replace("**", "").strip() if len(parts) > 0 else ""
            interview_time_str = parts[1].strip() if len(parts) > 1 else ""
            try:
                dt = datetime.strptime(interview_time_str, "%m/%d %H:%M")
            except Exception:
                dt = datetime.min
            return (interviewer_name, dt)

        dashboard_sections["日程調整済み"].sort(key=sort_key)
    dashboard_sections["プロフィール未記入"].sort()

    embeds = []
    for key, config in embed_config.items():
        entries = dashboard_sections.get(key, [])
        description = "\n".join(entries) if entries else "なし"
        embed = discord.Embed(
            title=config["title"],
            description=description,
            color=config["color"],
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text="最終更新: " + now.strftime("%Y-%m-%d %H:%M:%S"))
        embeds.append(embed)

    if data_manager.dashboard_message_id:
        try:
            msg = await dashboard_channel.fetch_message(data_manager.dashboard_message_id)
            await msg.edit(content="", embeds=embeds)
        except discord.NotFound:
            msg = await dashboard_channel.send(embeds=embeds)
            data_manager.dashboard_message_id = msg.id
    else:
        msg = await dashboard_channel.send(embeds=embeds)
        data_manager.dashboard_message_id = msg.id
    await data_manager.save_data()
    logger.info("ダッシュボード更新完了")

# ------------------------------------------------
# CandidateContext および補助関数
# ------------------------------------------------
@dataclass
class CandidateContext:
    candidate_id: int
    progress: Dict[str, Any]
    target_guild: discord.Guild
    target_member: discord.Member
    main_guild: discord.Guild
    interviewer: discord.Member
    progress_key: str


# ---------------- ここを丸ごと差し替え ----------------
async def get_candidate_context(
    interaction: discord.Interaction,
    progress_key_override: Optional[str] = None,
    candidate_id: Optional[int] = None
) -> Optional[CandidateContext]:
    """ボタン操作ごとに候補者・担当者・進捗データを取得し、権限もチェックして返す"""

    async def send_error(msg: str):
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)

    bot: discord.Client = interaction.client

    # ---------- progress_key & 進捗 -----------------
    progress_key = progress_key_override or \
                   data_manager.interview_channel_mapping.get(interaction.channel.id)
    if not progress_key:
        await send_error("候補者情報が見つかりません。")
        return None

    cp: Optional[Dict[str, Any]] = data_manager.candidate_progress.get(progress_key)
    if not cp:
        await send_error("進捗情報が見つかりません。")
        return None

    cid: int = cp.get("candidate_id", candidate_id)

    # ---------- ギルド探索順を再構成 -----------------
    guild_ids: List[int] = []
    source_gid: Optional[int] = cp.get("source_guild_id")

    # 1) まず progress に記録されたサーバー
    if source_gid:
        guild_ids.append(source_gid)

    # 2) ボタンが押されたサーバー
    if interaction.guild and interaction.guild.id not in guild_ids:
        guild_ids.append(interaction.guild.id)

    # 3) Bot が入っている残りのサーバー
    guild_ids.extend(g.id for g in bot.guilds if g.id not in guild_ids)

    # ---------- メンバー検索 -------------------------
    target_guild: Optional[discord.Guild] = None
    target_member: Optional[discord.Member] = None

    for gid in guild_ids:
        g = bot.get_guild(gid)
        if g is None:
            continue
        member = g.get_member(cid) or await utils.safe_fetch_member(g, cid)
        if member:
            target_guild, target_member = g, member
            # source_guild_id が未設定 / 変化していたら更新
            if source_gid != gid:
                cp["source_guild_id"] = gid
                await data_manager.save_data()
            break

    if target_member is None:
        await send_error("対象メンバーが見つかりません。")
        return None

    # ---------- 担当者チェック ------------------------
    interviewer_id: Optional[int] = cp.get("interviewer_id")
    if not interviewer_id:
        await send_error("担当者が設定されていません。")
        return None

    main_guild: Optional[discord.Guild] = bot.get_guild(MAIN_GUILD_ID)
    if not main_guild:
        await send_error("メインサーバーが見つかりません。")
        return None

    interviewer = main_guild.get_member(interviewer_id) or \
                  await utils.safe_fetch_member(main_guild, interviewer_id)
    interviewer_role: Optional[discord.Role] = main_guild.get_role(INTERVIEWER_ROLE_ID)
    if interviewer_role not in interviewer.roles:
        await send_error("操作権限がありません。")
        return None

    return CandidateContext(
        candidate_id=cid,
        progress=cp,
        target_guild=target_guild,
        target_member=target_member,
        main_guild=main_guild,
        interviewer=interviewer,
        progress_key=progress_key,
    )

# ------------------------------------------------
# ヘルパー関数（共通処理）
# ------------------------------------------------
async def update_memo_result_simple(target_member: discord.Member, result_str: str) -> None:
    candidate_key = str(target_member.id)
    if candidate_key in data_manager.memo_history and data_manager.memo_history[candidate_key]:
        data_manager.memo_history[candidate_key][-1]["result"] = result_str
        await data_manager.save_data()


async def process_immediate_action(
        interaction: discord.Interaction,
        context: CandidateContext,
        action_type: str
) -> None:
    """
    fail / ban / interval いずれも:
        ① 面接記録・進捗削除・統計更新
        ② ボタンを押したサーバー (target_guild) だけでキック
    ban / interval のときは BanManager へも記録を残す
    """
    candidate_id, cp, target_guild, target_member, main_guild, interviewer, progress_key = (
        context.candidate_id, context.progress, context.target_guild,
        context.target_member, context.main_guild, context.interviewer, context.progress_key
    )

    # ----- 理由 -----
    reasons = {
        "fail":     "面接不合格",
        "ban":      "即時BANにより入室不可",
        "interval": "即時インターバルにより入室不可"
    }
    reason = reasons.get(action_type)
    if not reason:
        return  # 想定外

    # ----- BAN / INTERVAL は BanManager にも記録 -----
    if action_type in ("ban", "interval"):
        ban_origin = "main" if target_guild.id == MAIN_GUILD_ID else "sub"
        ban_manager.add_ban(
            candidate_id,
            ban_origin,
            "BAN" if action_type == "ban" else "INTERVAL"
        )

    try:
        # ① 面接記録追加 & 進捗削除 ----------------------------------------
        data_manager.interview_records.append({
            "date":          get_current_time_iso(),
            "interviewer_id": cp["interviewer_id"],
            "interviewee_id": candidate_id,
            "result":         action_type.upper()
        })
        update_candidate_status(cp, action_type.upper())
        data_manager.candidate_progress.pop(progress_key, None)
        await data_manager.save_data()

        # ② ダッシュボード & 統計更新 --------------------------------------
        request_dashboard_update(interaction.client)
        asyncio.create_task(update_stats(interaction.client))
        await update_memo_result_simple(target_member, action_type.upper())

        # ③ **押されたサーバーだけでキック** -------------------------------
        kicked = False
        try:
            member = target_guild.get_member(candidate_id) or await target_guild.fetch_member(candidate_id)
            if member:
                await target_guild.kick(member, reason=reason)
                kicked = True
                logger.info(f"Guild {target_guild.id}: {action_type.upper()} で {candidate_id} をキック")
                await log_auto_kick(interaction.client, member, target_guild, reason)
        except discord.Forbidden:
            logger.warning(f"[権限不足] Guild {target_guild.id}: {candidate_id} をキックできません")
        except discord.HTTPException as e:
            logger.error(f"Guild {target_guild.id}: キック失敗 ({e})")

        # ④ 応答 -----------------------------------------------------------
        msg = (
            f"{target_member.mention} を **{action_type.upper()}** にしました。\n"
            f"{'✅ キック完了' if kicked else '⚠️ キックに失敗 / 既にいません'}"
        )
        await interaction.followup.send(msg, ephemeral=True)

    except Exception as e:
        logger.error(f"process_immediate_action 全体失敗: {e}", exc_info=True)


async def register_delayed_action(interaction: discord.Interaction, context: CandidateContext, action_type: str) -> None:
    candidate_id, cp, target_guild, target_member, main_guild, interviewer, progress_key = (
        context.candidate_id, context.progress, context.target_guild,
        context.target_member, context.main_guild, context.interviewer, context.progress_key
    )
    if action_type in ("ban", "interval"):
        ban_origin = "main" if target_guild.id == MAIN_GUILD_ID else "sub"
        ban_manager.add_ban(candidate_id, ban_origin, "BAN" if action_type == "ban" else "INTERVAL")
    now = datetime.now(JST)
    tomorrow = now.date() + timedelta(days=1)
    target_time = datetime.combine(tomorrow, time(9, 0, tzinfo=JST))
    action = {
        "id": str(uuid.uuid4()),
        "action_type": action_type,
        "candidate_id": candidate_id,
        "scheduled_time": target_time.isoformat(),
        "apply_all": True if target_guild.id == MAIN_GUILD_ID else False,
    }
    if target_guild.id != MAIN_GUILD_ID:
        action["guild_id"] = target_guild.id
    delayed_action_manager.add_action(action)
    await interaction.followup.send(f"{target_member.mention} の {action_type.upper()} 処理は翌日9時に実行されます。", ephemeral=True)
    update_candidate_status(cp, action_type.upper())
    data_manager.candidate_progress.pop(progress_key, None)
    await data_manager.save_data()
    request_dashboard_update(interaction.client)
    await update_memo_result_simple(target_member, action_type.upper() + " (遅延)")

    # 遅延処理の場合、実行時か登録時かどちらで記録を残すかはお好みで
    data_manager.interview_records.append({
        'date': get_current_time_iso(),
        'interviewer_id': cp['interviewer_id'],
        'interviewee_id': candidate_id,
        'result': action_type.upper() + " (遅延)"
    })
    await data_manager.save_data()

async def process_pass_action(interaction: discord.Interaction, context: CandidateContext) -> None:
    global transient_memo_cache
    candidate_id, cp, target_guild, target_member, main_guild, interviewer, progress_key = (
        context.candidate_id, context.progress, context.target_guild,
        context.target_member, context.main_guild, context.interviewer, context.progress_key
    )

    # 合格ロールの付与
    if target_guild.id == MAIN_GUILD_ID:
        pass_role = target_guild.get_role(PASS_ROLE_ID)
    else:
        pass_role = discord.utils.get(target_guild.roles, name=OTHER_SERVER_PASS_ROLE_NAME)

    if pass_role is None:
        await interaction.followup.send("合格ロールが見つかりませんでした。", ephemeral=True)
        logger.warning("合格ロールが見つかりませんでした。")
        return
    try:
        await target_member.add_roles(pass_role)
        logger.info(f"{target_member.mention} ({target_member.id}) に合格ロール {pass_role.name} ({pass_role.id}) を付与しました。")
    except discord.Forbidden:
        logger.error(f"合格ロール付与失敗: Botにロール管理権限がありません。 Role: {pass_role.name}, Member: {target_member.mention}")
        await interaction.followup.send("合格ロールの付与に失敗しました。Botの権限を確認してください。", ephemeral=True)
        return
    except discord.HTTPException as e:
        logger.error(f"合格ロール付与失敗 (HTTPException): {e}. Role: {pass_role.name}, Member: {target_member.mention}")
        await interaction.followup.send("合格ロールの付与中にエラーが発生しました。", ephemeral=True)
        return
    except Exception as e:
        logger.error(f"合格ロール付与失敗 (不明なエラー): {e}. Role: {pass_role.name}, Member: {target_member.mention}")
        await interaction.followup.send("合格ロールの付与中に予期せぬエラーが発生しました。", ephemeral=True)
        return

    # ステータス更新と面接記録の追加
    update_candidate_status(cp, "案内待ち")
    data_manager.interview_records.append({
        'date': get_current_time_iso(),
        'interviewer_id': cp['interviewer_id'],
        'interviewee_id': candidate_id,
        'result': 'pass'
    })

    # 合格通知 Embed の送信準備
    real_main_guild = interaction.client.get_guild(MAIN_GUILD_ID)
    if not real_main_guild:
        logger.error("メインサーバーが取得できません。")
        await interaction.followup.send("処理に必要なサーバー情報が見つかりません。", ephemeral=True)
        return

    pass_channel: Optional[discord.TextChannel] = real_main_guild.get_channel(PASS_MEMO_CHANNEL_ID)
    if not pass_channel:
        logger.error(f"合格メモ送信先チャンネルが見つかりませんでした。(ID:{PASS_MEMO_CHANNEL_ID})")
        await interaction.followup.send("合格通知の送信先チャンネルが見つかりません。", ephemeral=True)
        return

    memo_text = transient_memo_cache.pop(progress_key, "")
    desc_lines = memo_text.splitlines()
    filtered_lines = []
    for line in desc_lines:
        if "@@@@@@@@@@" in line:
            break
        filtered_lines.append(line)
    filtered_memo = "\n".join(filtered_lines)

    pass_embed = discord.Embed(
        description=f"{target_member.mention}\n{filtered_memo}",
        color=0x00FF00,
        timestamp=datetime.now(JST)
    )

    try:
        await pass_channel.send(embed=pass_embed)
        logger.info(f"合格メモ送信完了: {target_member.mention} ({target_member.id}) の合格通知を {pass_channel.mention} に送信")
        await interaction.followup.send(f"{target_member.mention} の合格処理が完了しました。", ephemeral=True)
    except discord.Forbidden:
        logger.error(f"合格メモ送信失敗: Botにチャンネルへの送信権限がありません。 Channel: {pass_channel.mention}")
        await interaction.followup.send("合格通知の送信に失敗しました。Botの権限を確認してください。", ephemeral=True)
    except discord.HTTPException as e:
        logger.error(f"合格メモ送信失敗 (HTTPException): {e}. Channel: {pass_channel.mention}")
        await interaction.followup.send("合格通知の送信中にエラーが発生しました。", ephemeral=True)
    except Exception as e:
        logger.error(f"合格メモ送信失敗 (不明なエラー): {e}. Channel: {pass_channel.mention}")
        await interaction.followup.send("合格通知の送信中に予期せぬエラーが発生しました。", ephemeral=True)

    await update_memo_result_simple(target_member, "合格")
    await data_manager.save_data()
    request_dashboard_update(interaction.client)

# ------------------------------------------------
# InterviewResultView（各ボタン付きView）
# ------------------------------------------------
class InterviewResultView(discord.ui.View):
    def __init__(self, progress_key: str) -> None:
        super().__init__(timeout=None)
        self.progress_key = progress_key

        pass_button = discord.ui.Button(label='合格', style=discord.ButtonStyle.success,
                                        custom_id=f'pass_button_{progress_key}')
        pass_button.callback = self.pass_button_callback
        self.add_item(pass_button)

        immediate_fail_button = discord.ui.Button(label='不合格', style=discord.ButtonStyle.red,
                                                  custom_id=f'fail_button_{progress_key}')
        immediate_fail_button.callback = self.immediate_fail_callback
        self.add_item(immediate_fail_button)

        immediate_ban_button = discord.ui.Button(label='BAN', style=discord.ButtonStyle.danger,
                                                 custom_id=f'ban_button_{progress_key}')
        immediate_ban_button.callback = self.immediate_ban_callback
        self.add_item(immediate_ban_button)

        immediate_interval_button = discord.ui.Button(label='インターバル', style=discord.ButtonStyle.secondary,
                                                      custom_id=f'interval_button_{progress_key}')
        immediate_interval_button.callback = self.immediate_interval_callback
        self.add_item(immediate_interval_button)

        delayed_fail_button = discord.ui.Button(label='[遅延]不合格', style=discord.ButtonStyle.red,
                                                custom_id=f'delayed_fail_button_{progress_key}')
        delayed_fail_button.callback = self.delayed_fail_callback
        self.add_item(delayed_fail_button)

        delayed_ban_button = discord.ui.Button(label='[遅延]BAN', style=discord.ButtonStyle.danger,
                                               custom_id=f'delayed_ban_button_{progress_key}')
        delayed_ban_button.callback = self.delayed_ban_callback
        self.add_item(delayed_ban_button)

        delayed_interval_button = discord.ui.Button(label='[遅延]インターバル', style=discord.ButtonStyle.secondary,
                                                    custom_id=f'delayed_interval_button_{progress_key}')
        delayed_interval_button.callback = self.delayed_interval_callback
        self.add_item(delayed_interval_button)

    async def pass_button_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await get_candidate_context(interaction, progress_key_override=self.progress_key)
        if context:
            await process_pass_action(interaction, context)
        try:
            await interaction.message.delete()
        except Exception as e:
            logger.error(f"ボタン付きメッセージ削除失敗: {e}")

    async def immediate_fail_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await get_candidate_context(interaction, progress_key_override=self.progress_key)
        if context:
            await process_immediate_action(interaction, context, "fail")
        try:
            await interaction.message.delete()
        except Exception as e:
            logger.error(f"ボタン付きメッセージ削除失敗: {e}")

    async def immediate_ban_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await get_candidate_context(interaction, progress_key_override=self.progress_key)
        if context:
            await process_immediate_action(interaction, context, "ban")
        try:
            await interaction.message.delete()
        except Exception as e:
            logger.error(f"ボタン付きメッセージ削除失敗: {e}")

    async def immediate_interval_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await get_candidate_context(interaction, progress_key_override=self.progress_key)
        if context:
            await process_immediate_action(interaction, context, "interval")
        try:
            await interaction.message.delete()
        except Exception as e:
            logger.error(f"ボタン付きメッセージ削除失敗: {e}")

    async def delayed_fail_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await get_candidate_context(interaction, progress_key_override=self.progress_key)
        if context:
            await register_delayed_action(interaction, context, "fail")
        try:
            await interaction.message.delete()
        except Exception as e:
            logger.error(f"ボタン付きメッセージ削除失敗: {e}")

    async def delayed_ban_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await get_candidate_context(interaction, progress_key_override=self.progress_key)
        if context:
            await register_delayed_action(interaction, context, "ban")
        try:
            await interaction.message.delete()
        except Exception as e:
            logger.error(f"ボタン付きメッセージ削除失敗: {e}")

    async def delayed_interval_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await get_candidate_context(interaction, progress_key_override=self.progress_key)
        if context:
            await register_delayed_action(interaction, context, "interval")
        try:
            await interaction.message.delete()
        except Exception as e:
            logger.error(f"ボタン付きメッセージ削除失敗: {e}")

# ------------------------------------------------
# VCControlView
# ------------------------------------------------
class VCControlView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label='[管理用]VC作成', style=discord.ButtonStyle.gray, custom_id='create_vc')
    async def create_vc(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        context = await get_candidate_context(interaction)
        if not context:
            return
        candidate_id, cp, target_guild, target_member, main_guild, interviewer, progress_key = (
            context.candidate_id, context.progress, context.target_guild,
            context.target_member, context.main_guild, context.interviewer, context.progress_key
        )
        if cp.get('voice_channel_id'):
            await interaction.response.send_message("VCは既に存在します。", ephemeral=True)
            return
        channel: Optional[discord.TextChannel] = interaction.client.get_channel(interaction.channel.id)
        if channel is None:
            await interaction.response.send_message("対象チャンネルが見つかりません。", ephemeral=True)
            return
        guild: discord.Guild = channel.guild
        interviewer_role: Optional[discord.Role] = main_guild.get_role(INTERVIEWER_ROLE_ID)
        voice_overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            target_member: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True,
                                                       use_voice_activation=True),
            interviewer_role: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True,
                                                          use_voice_activation=True) if interviewer_role else discord.PermissionOverwrite(),
            guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True,
                                                  use_voice_activation=True),
        }
        try:
            if guild.id == MAIN_GUILD_ID and channel.category:
                voice_channel: discord.VoiceChannel = await guild.create_voice_channel(
                    channel.name,
                    overwrites=voice_overwrites,
                    category=channel.category
                )
            else:
                voice_channel = await guild.create_voice_channel(
                    channel.name,
                    overwrites=voice_overwrites
                )
            logger.info(f"VC {voice_channel.id} 作成")
        except Exception as e:
            await interaction.response.send_message(f"VC作成失敗: {e}", ephemeral=True)
            return
        cp['voice_channel_id'] = voice_channel.id
        update_candidate_status(cp, "担当者待ち")
        await data_manager.save_data()
        request_dashboard_update(interaction.client)
        await interaction.response.send_message(f"VC {voice_channel.mention} 作成完了", ephemeral=True)

    @discord.ui.button(label='[管理用]VC削除', style=discord.ButtonStyle.gray, custom_id='delete_vc')
    async def delete_vc(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        context = await get_candidate_context(interaction)
        if not context:
            return
        candidate_id, cp, target_guild, target_member, main_guild, interviewer, progress_key = (
            context.candidate_id, context.progress, context.target_guild,
            context.target_member, context.main_guild, context.interviewer, context.progress_key
        )
        vc_channel_id: Optional[int] = cp.get('voice_channel_id')
        if vc_channel_id is None:
            await interaction.response.send_message("VCは存在しません。", ephemeral=True)
            return
        vc_channel: Optional[discord.VoiceChannel] = interaction.client.get_channel(vc_channel_id)
        if vc_channel:
            try:
                await vc_channel.delete()
                logger.info(f"VC {vc_channel_id} 削除")
            except Exception as e:
                await interaction.response.send_message("VC削除失敗", ephemeral=True)
                return
        cp.pop('voice_channel_id', None)
        await data_manager.save_data()
        request_dashboard_update(interaction.client)
        await interaction.response.send_message("VC削除完了", ephemeral=True)

    @discord.ui.button(label='[管理用]日時設定/変更', style=discord.ButtonStyle.gray, custom_id='schedule_interview')
    async def schedule_interview(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        context = await get_candidate_context(interaction)
        if not context:
            return
        candidate_id, cp, target_guild, target_member, main_guild, interviewer, progress_key = (
            context.candidate_id, context.progress, context.target_guild,
            context.target_member, context.main_guild, context.interviewer, context.progress_key
        )
        modal = ScheduleModal(progress_key, cp.get('interviewer_id'))
        await interaction.response.send_modal(modal)
        logger.info("日時設定モーダル表示")

    @discord.ui.button(label='[管理用]開始', style=discord.ButtonStyle.gray, custom_id='submit_memo')
    async def submit_memo(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        context = await get_candidate_context(interaction)
        if not context:
            return
        candidate_id, cp, target_guild, target_member, main_guild, interviewer, progress_key = (
            context.candidate_id, context.progress, context.target_guild,
            context.target_member, context.main_guild, context.interviewer, context.progress_key
        )
        modal = MemoModal(progress_key, cp.get('interviewer_id'), cp.get('source_guild_id', MAIN_GUILD_ID))
        await interaction.response.send_modal(modal)
        logger.info("面接メモ入力モーダル表示")

# ------------------------------------------------
# MemoModal
# ------------------------------------------------
class MemoModal(discord.ui.Modal, title="面接メモの入力"):
    memo: discord.ui.TextInput = discord.ui.TextInput(
        label="面接メモ", style=discord.TextStyle.paragraph, required=True
    )

    def __init__(self, progress_key: str, interviewer_id: int, source_guild_id: int) -> None:
        super().__init__()
        self.progress_key = progress_key
        self.interviewer_id = interviewer_id
        self.source_guild_id = source_guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        global transient_memo_cache
        await interaction.response.defer(ephemeral=True)

        # ---- 進捗 / 候補者コンテキスト取得 ---------------------------------
        context = await get_candidate_context(
            interaction, progress_key_override=self.progress_key
        )
        if not context:
            return

        candidate_id, cp, target_guild, target_member, main_guild, interviewer, progress_key = (
            context.candidate_id,
            context.progress,
            context.target_guild,
            context.target_member,
            context.main_guild,
            context.interviewer,
            context.progress_key,
        )

        bot: discord.Client = interaction.client
        if main_guild is None:
            await interaction.followup.send("メインサーバーが見つかりません。", ephemeral=True)
            return

        additional_channel: Optional[discord.TextChannel] = main_guild.get_channel(ADDITIONAL_MEMO_CHANNEL_ID)
        button_channel:    Optional[discord.TextChannel] = main_guild.get_channel(INTERVIEW_MEMO_CHANNEL_ID)
        if additional_channel is None and button_channel is None:
            await interaction.followup.send("面接メモの送信先チャンネルが見つかりません。", ephemeral=True)
            return

        # ---- 候補者オブジェクト取得 ----------------------------------------
        interviewee: Optional[discord.Member] = main_guild.get_member(candidate_id)
        if interviewee is None:
            try:
                interviewee = await main_guild.fetch_member(candidate_id)
            except discord.NotFound:
                await interaction.followup.send("候補者が見つかりません。", ephemeral=True)
                return

        # 担当者が本人以外の場合は上書き
        if interaction.user.id != self.interviewer_id:
            cp['interviewer_id'] = interaction.user.id
            self.interviewer_id  = interaction.user.id

        # ---- Embed 作成 ----------------------------------------------------
        embed = discord.Embed(description=self.memo.value)
        embed.set_author(name=str(interviewee), icon_url=interviewee.display_avatar.url)

        updated_interviewer: Optional[discord.Member] = main_guild.get_member(cp.get('interviewer_id'))
        interviewer_name = updated_interviewer.display_name if updated_interviewer else "不明"

        # ★ ここを追加: 候補者ユーザーIDをフッターに追記
        embed.set_footer(
            text=(
                f"面接担当者: {interviewer_name}\n"
                f"候補者ユーザーID: {candidate_id}"
            )
        )

        # ---- 送信処理 ------------------------------------------------------
        memo_msg: Optional[discord.Message] = None
        if additional_channel and button_channel and (additional_channel.id == button_channel.id):
            memo_msg = await button_channel.send(
                embed=embed,
                view=InterviewResultView(self.progress_key)
            )
        else:
            if additional_channel:
                memo_msg = await additional_channel.send(embed=embed)
            if button_channel:
                await button_channel.send(
                    embed=embed,
                    view=InterviewResultView(self.progress_key)
                )

        await interaction.followup.send("面接メモ送信完了", ephemeral=True)

        # ---- 進捗 & メモ履歴更新 ------------------------------------------
        update_candidate_status(cp, "面接済み")
        cp['profile_filled_time'] = get_current_time_iso()
        await data_manager.save_data()

        transient_memo_cache[self.progress_key] = self.memo.value

        if memo_msg is not None:
            candidate_key = str(interviewee.id)
            history = data_manager.memo_history.get(candidate_key, [])
            history.append({
                "guild_id":   main_guild.id,
                "channel_id": memo_msg.channel.id,
                "message_id": memo_msg.id,
                "timestamp":  get_current_time_iso(),
                "result":     "未評価",
                "memo_text":  self.memo.value,
            })
            data_manager.memo_history[candidate_key] = history
            await data_manager.save_data()

        request_dashboard_update(bot)
        logger.info("面接メモ処理完了")

# ------------------------------------------------
# ScheduleModal
# ------------------------------------------------
class ScheduleModal(discord.ui.Modal, title="面接日時の入力"):
    interview_time: discord.ui.TextInput = discord.ui.TextInput(label="面接開始時間（HHMM）", placeholder="例: 2130",
                                                                required=True)
    interview_date: discord.ui.TextInput = discord.ui.TextInput(label="面接日付（MMDD、空白なら当日）",
                                                                placeholder="例: 0131", required=False)

    def __init__(self, progress_key: str, interviewer_id: int) -> None:
        super().__init__()
        self.progress_key = progress_key
        self.interviewer_id = interviewer_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        time_str: str = self.interview_time.value.strip()
        date_str: str = self.interview_date.value.strip()
        current_year: int = datetime.now(JST).year
        if not date_str:
            date_str = datetime.now(JST).strftime("%m%d")
        datetime_str: str = f"{current_year} {date_str} {time_str}"
        try:
            dt: datetime = datetime.strptime(datetime_str, "%Y %m%d %H%M")
            dt = dt.replace(tzinfo=JST)
        except Exception:
            await interaction.response.send_message("入力形式が正しくありません。", ephemeral=True)
            return
        if dt < datetime.now(JST):
            await interaction.response.send_message("未来の日時を入力してください。", ephemeral=True)
            return
        cp: Optional[Dict[str, Any]] = data_manager.candidate_progress.get(self.progress_key)
        if cp:
            cp['interview_time'] = dt.isoformat()
            cp['interviewer_id'] = self.interviewer_id
            cp['scheduled_time'] = get_current_time_iso()
            cp['notified_candidate'] = False
            cp['notified_interviewer'] = False
            update_candidate_status(cp, "日程調整済み")
            await data_manager.save_data()
            request_dashboard_update(interaction.client)
        await interaction.response.send_message("面接日時設定完了", ephemeral=True)

# ------------------------------------------------
# 月次カウント機能（Cog）
# ------------------------------------------------
class MonthlyCountCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.monthly_counts_data: Dict[str, Dict[int, Dict[str, Any]]] = {}
        self.monthly_messages: Dict[str, Optional[discord.Message]] = {}
        self.current_year_month: Optional[str] = None
        self.load_counts_data()

    def load_counts_data(self):
        if os.path.isfile(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                for ym, data in raw_data.items():
                    self.monthly_counts_data[ym] = {}
                    for exec_id_str, info in data.items():
                        try:
                            exec_id_int = int(exec_id_str)
                            self.monthly_counts_data[ym][exec_id_int] = {
                                "name": info.get("name", "不明な担当者"),
                                "assigned": set(info.get("assigned", []))
                            }
                        except ValueError:
                            logger.warning(f"データロード中に無効な担当者IDをスキップ: {exec_id_str} in {ym}")
                logger.info("月次カウントデータのロードに成功しました。")
            except json.JSONDecodeError as e:
                logger.error(f"月次カウントデータファイル({DATA_FILE})のJSON解析エラー: {e}")
            except Exception as e:
                logger.error(f"月次カウントデータのロード中に予期せぬエラー: {e}", exc_info=True)
        else:
            logger.info("月次カウント用の保存済みデータファイルが見つかりません。新規に開始します。")

        now = datetime.now(JST)
        ym = f"{now.year}-{now.month:02d}"
        if self.current_year_month is None:
            self.current_year_month = ym
            if ym not in self.monthly_counts_data:
                 self.monthly_counts_data[ym] = {}

    def save_counts_data(self):
        to_save = {}
        for ym, data in self.monthly_counts_data.items():
            to_save[ym] = {}
            for exec_id, info in data.items():
                to_save[ym][str(exec_id)] = {
                    "name": info.get("name", "不明な担当者"),
                    "assigned": list(info.get("assigned", set()))
                }
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(to_save, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"月次カウントデータの保存中にエラー: {e}", exc_info=True)

    async def check_monthly_reset(self):
        now = datetime.now(JST)
        ym = f"{now.year}-{now.month:02d}"
        if self.current_year_month != ym:
            logger.info(f"月が替わりました: {self.current_year_month} -> {ym}")
            self.current_year_month = ym
            if ym not in self.monthly_counts_data:
                self.monthly_counts_data[ym] = {}
            self.monthly_messages[ym] = None
            channel = self.bot.get_channel(LOG_CHANNEL_ID)
            if channel and isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(f"✨ **{ym}** の案内カウントを開始します。")
                except discord.Forbidden:
                    logger.error(f"チャンネル {LOG_CHANNEL_ID} への月替わり通知送信権限がありません。")
                except Exception as e:
                    logger.error(f"チャンネル {LOG_CHANNEL_ID} への月替わり通知送信中にエラー: {e}")
            else:
                logger.warning(f"月替わり通知用のチャンネル {LOG_CHANNEL_ID} が見つからないか、テキストチャンネルではありません。")
            await self.update_log_message()
            self.save_counts_data()

    async def update_log_message(self):
        # 実装略
        pass


# ------------------------------------------------
# EventCog（イベントハンドラ）
# ------------------------------------------------
class EventCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild.id != MAIN_GUILD_ID:
            ban_record = ban_manager.check_ban(member.id)
            if ban_record:
                try:
                    await member.guild.kick(member, reason="BAN/インターバルにより入室不可")
                    logger.info(f"Member {member.id} を guild {member.guild.id} からキック（BAN/インターバル適用）")
                    # ★ 追記: 自動キックログ
                    await log_auto_kick(self.bot, member, member.guild, "BAN/インターバルにより入室不可")
                except Exception as e:
                    logger.error(f"Member {member.id} のキック失敗（guild {member.guild.id}）：{e}")
                return
        if member.guild.id != MAIN_GUILD_ID:
            for cp in data_manager.candidate_progress.values():
                if cp.get("candidate_id") == member.id and cp.get("source_guild_id") != member.guild.id:
                    try:
                        await member.guild.kick(member, reason="既に面接部屋が存在するため、他のサーバーへの参加は禁止")
                        logger.info(f"Member {member.id} を guild {member.guild.id} からキック（既存面接部屋検出）")
                        # ★ 追記: 自動キックログ
                        await log_auto_kick(self.bot, member, member.guild, "既に面接部屋が存在するためキック")
                    except Exception as e:
                        logger.error(f"Member {member.id} のキック失敗（guild {member.guild.id}）：{e}")
                    return

        guild: discord.Guild = member.guild
        channel_name: str = f"面接部屋-{member.display_name}"
        is_main_guild: bool = (guild.id == MAIN_GUILD_ID)
        interviewer_role: Optional[discord.Role] = get_interviewer_role(guild)
        if interviewer_role is None:
            interviewer_role = guild.default_role
            logger.warning("面接担当者ロールが見つからなかったので default_role を使用")

        text_overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            interviewer_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        try:
            if is_main_guild:
                category: Optional[discord.CategoryChannel] = guild.get_channel(MAIN_CATEGORY_ID)
                if category:
                    interview_channel: discord.TextChannel = await guild.create_text_channel(
                        channel_name, overwrites=text_overwrites, category=category
                    )
                else:
                    interview_channel = await guild.create_text_channel(channel_name, overwrites=text_overwrites)
            else:
                interview_channel = await guild.create_text_channel(channel_name, overwrites=text_overwrites)
        except Exception as e:
            logger.error(f"面接チャンネル作成失敗: {e}")
            return

        progress_key = make_progress_key(guild.id, member.id)
        data_manager.interview_channel_mapping[interview_channel.id] = progress_key
        data_manager.candidate_progress[progress_key] = {
            'candidate_id': member.id,
            'status': "プロフィール未記入",
            'channel_id': interview_channel.id,
            'source_guild_id': guild.id,
            'timestamp': get_current_time_iso(),
            'interview_time': None,
            'interviewer_id': None,
            'join_time': get_current_time_iso(),
            'profile_filled_time': None,
            'scheduled_time': None,
            'notified_candidate': False,
            'notified_interviewer': False,
            'notify_time': None,
            'failed': False
        }
        await data_manager.save_data()
        request_dashboard_update(self.bot)
        logger.info(f"候補者 {member.id} の進捗初期化完了")

        try:
            await interview_channel.send(content='\u200b', view=VCControlView())
        except Exception as e:
            logger.error(f"VCControlView送信失敗: {e}")

        await interview_channel.send(f"{member.mention} さん、当会議に面接にお越しいただき、ありがとうございます✨")

        main_guild: Optional[discord.Guild] = self.bot.get_guild(MAIN_GUILD_ID)
        if main_guild:
            source_channel: Optional[discord.TextChannel] = main_guild.get_channel(PROFILE_FORM_CHANNEL_ID)
            if source_channel:
                messages: List[str] = []
                try:
                    async for msg in source_channel.history(limit=2, oldest_first=True):
                        messages.append(msg.content)
                except Exception as e:
                    logger.error(f"プロフィールフォーム取得失敗: {e}")
                    await interview_channel.send("プロフィールフォーム取得中にエラー")
                    return
                if messages:
                    for profile_form in messages:
                        try:
                            await interview_channel.send(profile_form)
                        except Exception as e:
                            logger.error(f"プロフィールフォーム送信失敗: {e}")
                else:
                    await interview_channel.send("プロフィールフォームが見つかりませんでした。")
            else:
                await interview_channel.send("プロフィールフォームチャンネルが見つかりませんでした。")
        else:
            await interview_channel.send("メインサーバーが見つかりませんでした。")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles != after.roles:
            specific_role: Optional[discord.Role] = after.guild.get_role(SPECIFIC_ROLE_ID)
            if specific_role in after.roles and specific_role not in before.roles:
                await delete_candidate_channels(after.guild, after.id)

            if after.guild.id == MAIN_GUILD_ID:
                pass_role: Optional[discord.Role] = after.guild.get_role(PASS_ROLE_ID)
            else:
                from discord.utils import get
                pass_role = get(after.guild.roles, name=OTHER_SERVER_PASS_ROLE_NAME)

            if pass_role:
                if pass_role in before.roles and pass_role not in after.roles:
                    progress_key = make_progress_key(after.guild.id, after.id)
                    if progress_key in data_manager.candidate_progress and \
                            data_manager.candidate_progress[progress_key]['status'] == "案内待ち":
                        data_manager.candidate_progress.pop(progress_key, None)
                        await data_manager.save_data()
                        await data_manager.save_data()
                        request_dashboard_update(self.bot)
                if pass_role in after.roles and pass_role not in before.roles:
                    await delete_candidate_channels(after.guild, after.id)
                    progress_key = make_progress_key(after.guild.id, after.id)
                    if progress_key in data_manager.candidate_progress:
                        data_manager.candidate_progress.pop(progress_key, None)
                        await data_manager.save_data()
                        request_dashboard_update(self.bot)

            interviewer_role: Optional[discord.Role] = after.guild.get_role(INTERVIEWER_ROLE_ID)
            if interviewer_role:
                if interviewer_role in after.roles and interviewer_role not in before.roles:
                    await update_stats(self.bot)
                elif interviewer_role not in after.roles and interviewer_role in before.roles:
                    await update_stats(self.bot)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        data_manager.interview_channel_mapping.pop(channel.id, None)
        for progress_key, cp in list(data_manager.candidate_progress.items()):
            if cp.get('channel_id') == channel.id:
                data_manager.candidate_progress.pop(progress_key, None)
                await data_manager.save_data()
                request_dashboard_update(self.bot)
                logger.info(f"候補者 {progress_key} の進捗削除 (チャンネル削除)")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild: discord.Guild = member.guild
        await delete_candidate_channels(guild, member.id)
        progress_key = make_progress_key(guild.id, member.id)
        data_manager.candidate_progress.pop(progress_key, None)
        await data_manager.save_data()
        request_dashboard_update(self.bot)
        logger.info(f"メンバー {member.id} 退会処理完了")

        interviewer_role: Optional[discord.Role] = guild.get_role(INTERVIEWER_ROLE_ID)
        if interviewer_role and interviewer_role in member.roles:
            await update_stats(self.bot)

        cp: Optional[Dict[str, Any]] = data_manager.candidate_progress.get(progress_key)
        if cp and cp.get('interviewer_id') and not cp.get('failed', False):
            interviewer: Optional[discord.Member] = guild.get_member(cp.get('interviewer_id'))
            if interviewer:
                try:
                    await interviewer.send(f"候補者 {member.display_name} が退出しました。")
                except Exception as e:
                    logger.error(f"担当者へのDM送信失敗: {e}")


# ------------------------------------------------
# TaskCog（定期タスク）
# ------------------------------------------------
class TaskCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self.check_candidate_status.start()
        self.schedule_notifications.start()

    @tasks.loop(minutes=5)
    async def check_candidate_status(self) -> None:
        logger.info("check_candidate_status タスク開始")
        now: datetime = datetime.now(JST)
        for progress_key, cp in list(data_manager.candidate_progress.items()):
            if cp.get('status') != "プロフィール未記入":
                continue
            join_time_str: Optional[str] = cp.get('join_time')
            if not join_time_str:
                continue
            try:
                join_time: datetime = datetime.fromisoformat(join_time_str)
            except Exception as e:
                logger.error(f"join_time チェック失敗: {e}")
                continue

            channel = await ensure_channel_exists(self.bot, progress_key, cp)
            if channel is None:
                continue

            candidate_id = cp.get("candidate_id")
            candidate: Optional[discord.User] = self.bot.get_user(candidate_id)
            if candidate is None:
                continue

            if now - join_time >= timedelta(hours=6) and not cp.get("profile_notification_6h_sent", False):
                await channel.send(f"{candidate.mention} プロフィール記入をお願いします。")
                cp["profile_notification_6h_sent"] = True
                await data_manager.save_data()

            if now - join_time >= timedelta(hours=24) and not cp.get("profile_warning_24h_sent", False):
                await channel.send(
                    f"{candidate.mention} 本日中にプロフィール記入がされない場合はキックとなります。")
                cp["profile_warning_24h_sent"] = True
                await data_manager.save_data()

            if now - join_time >= timedelta(hours=36):
                guild_id = cp.get("source_guild_id", MAIN_GUILD_ID)
                guild = self.bot.get_guild(guild_id)
                if guild:
                    member_obj = guild.get_member(candidate_id)
                    if member_obj:
                        try:
                            await guild.kick(member_obj, reason="プロフィール未記入による自動キック")
                            logger.info(f"候補者 {candidate_id} を自動キックしました。")
                            # ★ 追記: 自動キックログ
                            await log_auto_kick(self.bot, member_obj, guild, "プロフィール未記入による自動キック")
                            data_manager.candidate_progress.pop(progress_key, None)
                            await data_manager.save_data()
                        except Exception as e:
                            logger.error(f"自動キック失敗: {e}")

        logger.info("check_candidate_status タスク完了")

    @tasks.loop(minutes=1)
    async def schedule_notifications(self) -> None:
        now: datetime = datetime.now(JST)
        for progress_key, cp in list(data_manager.candidate_progress.items()):
            candidate_id = cp.get('candidate_id')
            member: Optional[discord.User] = self.bot.get_user(candidate_id)
            if not member:
                continue
            channel = await ensure_channel_exists(self.bot, progress_key, cp)
            if channel is None:
                continue

            if cp['status'] in ["日程調整済み", "面接済み"]:
                interview_time: Optional[str] = cp.get('interview_time')
                interviewer_id: Optional[int] = cp.get('interviewer_id')
                scheduled_time: Optional[str] = cp.get('scheduled_time')
                if not interview_time or not interviewer_id or not scheduled_time:
                    continue
                try:
                    it: datetime = datetime.fromisoformat(interview_time)
                    st: datetime = datetime.fromisoformat(scheduled_time)
                except Exception as e:
                    logger.error(f"日時解析失敗: {e}")
                    continue
                if it <= now:
                    continue
                if (it - now) <= timedelta(hours=1) and not cp.get('notified_candidate') and (it - st) >= timedelta(hours=1):
                    await channel.send(f"{member.mention} 面接開始1時間前です。")
                    cp['notified_candidate'] = True
                    await data_manager.save_data()
                if (it - now) <= timedelta(minutes=10) and not cp.get('notified_interviewer'):
                    interviewer: Optional[discord.User] = self.bot.get_user(interviewer_id)
                    if interviewer:
                        try:
                            await interviewer.send(f"面接開始10分前です。候補者: {member.mention}")
                            cp['notified_interviewer'] = True
                            await data_manager.save_data()
                        except Exception as e:
                            logger.error(f"10分前リマインド失敗: {e}")

                if now >= it + timedelta(minutes=1):
                    cp['notified_candidate'] = False
                    cp['notified_interviewer'] = False
                    await data_manager.save_data()

# ------------------------------------------------
# DelayedActionManager（遅延アクション管理）
# ------------------------------------------------
class DelayedActionManager:
    def __init__(self, file_path: str = "delayed_actions.json"):
        self.file_path = file_path
        self.actions = []  # {"id", "action_type", "candidate_id", "scheduled_time", "apply_all", "guild_id" (optional)}
        self.load_actions()

    def load_actions(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    self.actions = json.load(f)
                logger.info("Delayed actions のロードに成功しました。")
            except Exception as e:
                logger.error(f"Delayed actions ロード失敗: {e}")
                self.actions = []
        else:
            self.actions = []

    def save_actions(self):
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.actions, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Delayed actions 保存失敗: {e}")

    def add_action(self, action: dict):
        self.actions.append(action)
        self.save_actions()

    def remove_action(self, action_id: str):
        self.actions = [a for a in self.actions if a.get("id") != action_id]
        self.save_actions()

    def get_due_actions(self, now: datetime):
        due = []
        for action in self.actions:
            try:
                scheduled_time = datetime.fromisoformat(action["scheduled_time"])
                if scheduled_time <= now:
                    due.append(action)
            except Exception as e:
                logger.error(f"日時解析エラー: {e}")
        return due

delayed_action_manager = DelayedActionManager()

async def execute_delayed_action(action: dict, bot: commands.Bot):
    candidate_id = action["candidate_id"]
    action_type = action["action_type"]
    if action_type == "ban":
        reason = "BAN (遅延キック)"
    elif action_type == "fail":
        reason = "面接不合格 (遅延キック)"
    elif action_type == "interval":
        reason = "インターバル (遅延キック)"
    else:
        logger.error(f"不明なアクション種別: {action_type}")
        return
    apply_all = action.get("apply_all", False)

    if apply_all:
        for guild in bot.guilds:
            try:
                member = guild.get_member(candidate_id) or await guild.fetch_member(candidate_id)
                if member:
                    try:
                        await guild.kick(member, reason=reason)
                        logger.info(f"Guild {guild.id} で候補者 {candidate_id} に対して {action_type} 遅延処理実行")
                        # ★ 追記: 自動キックログ
                        await log_auto_kick(bot, member, guild, reason)
                    except Exception as e:
                        logger.error(f"Guild {guild.id} での遅延処理 {action_type} 失敗: {e}")
            except Exception as e:
                logger.error(f"Guild {guild.id} で候補者 {candidate_id} の取得失敗: {e}")
    else:
        guild_id = action.get("guild_id")
        if guild_id is not None:
            guild = bot.get_guild(guild_id)
            if guild:
                try:
                    member = guild.get_member(candidate_id) or await guild.fetch_member(candidate_id)
                    if member:
                        try:
                            await guild.kick(member, reason=reason)
                            logger.info(f"Guild {guild_id} で候補者 {candidate_id} に対して {action_type} 遅延処理実行")
                            # ★ 追記: 自動キックログ
                            await log_auto_kick(bot, member, guild, reason)
                        except Exception as e:
                            logger.error(f"Guild {guild_id} での遅延処理 {action_type} 失敗: {e}")
                except Exception as e:
                    logger.error(f"Guild {guild_id} で候補者 {candidate_id} の取得失敗: {e}")

# ------------------------------------------------
# DelayedActionCog（定期タスクで遅延アクションをチェック・実行）
# ------------------------------------------------
class DelayedActionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.check_delayed_actions.start()

    @tasks.loop(seconds=30)
    async def check_delayed_actions(self):
        logger.info(f"[DelayedAction] check start: {datetime.now(JST).isoformat()}")
        now = datetime.now(JST)
        due_actions = delayed_action_manager.get_due_actions(now)
        if due_actions:
            for action in due_actions:
                await execute_delayed_action(action, self.bot)
                delayed_action_manager.remove_action(action.get("id"))

    @check_delayed_actions.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

# ------------------------------------------------
# MessageCog
# ------------------------------------------------
class MessageCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        channel_id: int = message.channel.id
        if channel_id in data_manager.interview_channel_mapping:
            progress_key = data_manager.interview_channel_mapping[channel_id]
            try:
                _, real_candidate_id_str = progress_key.split("-")
                real_candidate_id = int(real_candidate_id_str)
            except Exception:
                real_candidate_id = None
            cp = data_manager.candidate_progress.get(progress_key)
            if real_candidate_id and message.author.id == real_candidate_id:
                if cp and cp.get('status') == "プロフィール未記入":
                    first_line = message.content.splitlines()[0] if message.content else ""
                    if "名前" in first_line:
                        update_candidate_status(cp, "記入済み")
                        cp['profile_filled_time'] = get_current_time_iso()
                        await data_manager.save_data()
                        await message.reply("プロフィール記入ありがとうございます。面接官が確認次第ご連絡します。")
                        now_hour: int = datetime.now(JST).hour
                        remind_channel: Optional[discord.TextChannel] = self.bot.get_channel(INTERVIEWER_REMIND_CHANNEL_ID)
                        if now_hour >= 23 or now_hour < 7:
                            if now_hour < 7:
                                next_notify_time: datetime = datetime.now(JST).replace(hour=7, minute=0, second=0, microsecond=0)
                                cp['notify_time'] = next_notify_time.isoformat()
                                await data_manager.save_data()
                            else:
                                if remind_channel:
                                    await send_interviewer_notification(self.bot, remind_channel, message.channel)
                        else:
                            if remind_channel:
                                await send_interviewer_notification(self.bot, remind_channel, message.channel)
            else:
                role = get_interviewer_role(message.guild)
                if role and role in message.author.roles:
                    cp = data_manager.candidate_progress.get(data_manager.interview_channel_mapping.get(channel_id))
                    if cp and cp.get('interviewer_id') is None:
                        cp['interviewer_id'] = message.author.id
                        update_candidate_status(cp, "担当者待ち")
                        await data_manager.save_data()
                        request_dashboard_update(self.bot)
        else:
            role = get_interviewer_role(message.guild)
            if role and role in message.author.roles:
                cp = data_manager.candidate_progress.get(data_manager.interview_channel_mapping.get(channel_id))
                if cp and cp.get('interviewer_id') is None:
                    cp['interviewer_id'] = message.author.id
                    update_candidate_status(cp, "担当者待ち")
                    await data_manager.save_data()
                    request_dashboard_update(self.bot)
        await self.bot.process_commands(message)

# ------------------------------------------------
# AdminCog（管理者用コマンド）
# ------------------------------------------------
class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="update_stats", description="統計を手動更新して出力します")
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    async def update_stats_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await update_stats(self.bot)
        await interaction.followup.send("統計情報を更新しました。", ephemeral=True)

    @app_commands.command(name="remove_ban", description="対象ユーザーのBAN／インターバルを手動で解除します（メインサーバー専用）")
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    async def remove_ban_command(self, interaction: discord.Interaction, target: discord.Member):
        if interaction.guild.id != MAIN_GUILD_ID:
            await interaction.response.send_message("このコマンドはメインサーバーでのみ使用可能です。", ephemeral=True)
            return
        ban_record = ban_manager.check_ban(target.id)
        if not ban_record:
            await interaction.response.send_message(f"{target.mention} は現在BAN／インターバル状態ではありません。", ephemeral=True)
            return
        ban_manager.remove_ban(target.id)
        await interaction.response.send_message(f"{target.mention} のBAN／インターバルを手動で解除しました。", ephemeral=True)

    @app_commands.command(name="ban_list", description="現在のBAN／インターバル対象の一覧を表示します（メインサーバー専用）")
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    async def ban_list_command(self, interaction: discord.Interaction):
        if interaction.guild.id != MAIN_GUILD_ID:
            await interaction.response.send_message("このコマンドはメインサーバーでのみ使用可能です。", ephemeral=True)
            return
        if not ban_manager.ban_records:
            await interaction.response.send_message("現在、BAN／インターバル状態のユーザーは存在しません。", ephemeral=True)
            return
        lines = []
        for user_id_str, record in ban_manager.ban_records.items():
            user_id = int(user_id_str)
            member = interaction.guild.get_member(user_id)
            name = member.display_name if member else f"ID:{user_id}"
            ban_type = record.get("ban_type", "Unknown")
            ban_origin = record.get("ban_origin", "Unknown")
            ban_time = record.get("ban_time", "Unknown")
            lines.append(f"{name} - {ban_type} (origin: {ban_origin}) - {ban_time}")
        output = "\n".join(lines)
        await interaction.response.send_message(f"現在のBAN／インターバル対象一覧:\n{output}", ephemeral=True)

    # ------------------------------------------------
    # ★ 追加: /add_manual_count コマンド (面接回数を強制セット)
    # ------------------------------------------------
    @app_commands.command(name="add_manual_count", description="面接官の面接回数を指定した値に手動で設定（上書き）します。")
    @app_commands.checks.has_role(ADMIN_ROLE_ID)
    @app_commands.describe(interviewer="回数を設定する面接官メンバー", count="設定する回数 (0以上の整数)")
    async def add_manual_count_command(self, interaction: discord.Interaction, interviewer: discord.Member, count: app_commands.Range[int, 0]):
        await interaction.response.defer(ephemeral=True)
        if count < 0:
            await interaction.followup.send("回数には0以上の整数を入力してください。", ephemeral=True)
            return
        try:
            interviewer_id = interviewer.id
            logger.info(f"コマンド実行: /add_manual_count interviewer={interviewer.display_name}({interviewer_id}) count={count} by {interaction.user.display_name}")
            original_length = len(data_manager.interview_records)
            new_interview_records = [
                record for record in data_manager.interview_records
                if record.get('interviewer_id') != interviewer_id
            ]
            removed_count = original_length - len(new_interview_records)
            if removed_count > 0:
                logger.info(f"手動設定準備: {interviewer.mention}({interviewer_id}) の既存の面接記録 {removed_count} 件を削除しました。")

            added_count = 0
            for i in range(count):
                dummy_record = {
                    'date': get_current_time_iso(),
                    'interviewer_id': interviewer_id,
                    'interviewee_id': f"manual_set_{i+1}",
                    'result': 'manual_set'
                }
                new_interview_records.append(dummy_record)
                added_count += 1

            data_manager.interview_records = new_interview_records
            await data_manager.save_data()
            await update_stats(self.bot)

            logger.info(f"手動面接カウント設定完了: {interviewer.mention}({interviewer_id}) -> {added_count} 回に設定")
            await interaction.followup.send(
                f"{interviewer.mention} の面接回数を **{added_count} 回** に設定し、統計を更新しました。", ephemeral=True)

        except Exception as e:
            logger.error(f"/add_manual_count コマンド実行中にエラー: {e}", exc_info=True)
            await interaction.followup.send("面接回数の設定中に予期せぬエラーが発生しました。", ephemeral=True)

    @add_manual_count_command.error
    async def add_manual_count_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        error_message: str = "コマンドの実行中にエラーが発生しました。"
        log_level: int = logging.ERROR

        if isinstance(error, app_commands.MissingRole):
            error_message = "このコマンドを実行する権限がありません。"
            log_level = logging.WARNING
        elif isinstance(error, app_commands.RangeError):
            error_message = "回数には0以上の整数を入力してください。"
            log_level = logging.WARNING
        elif isinstance(error.original, discord.NotFound):
            error_message = "応答中に問題が発生しました。時間をおいて再度お試しください。"
            log_level = logging.WARNING

        logger.log(log_level, f"/add_manual_count コマンドエラー: {error}", exc_info=(log_level == logging.ERROR))

        if interaction.response.is_done():
            try:
                await interaction.followup.send(error_message, ephemeral=True)
            except Exception as e_followup:
                logger.error(f"/add_manual_count followup送信エラー: {e_followup}")
        else:
            try:
                await interaction.response.send_message(error_message, ephemeral=True)
            except Exception as e_response:
                logger.error(f"/add_manual_count response送信エラー: {e_response}")


# ------------------------------------------------
# Bot本体（Cog登録など）
# ------------------------------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

class MyBot(commands.Bot):
    async def setup_hook(self) -> None:
        await self.add_cog(EventCog(self))
        await self.add_cog(TaskCog(self))
        await self.add_cog(AdminCog(self))
        await self.add_cog(MessageCog(self))
        await self.add_cog(DelayedActionCog(self))
        await self.add_cog(MonthlyCountCog(self))
        self.add_view(VCControlView())

    async def on_ready(self) -> None:
        await self.tree.sync()
        logger.info(f'Logged in as {self.user.name}')
        for progress_key, cp in data_manager.candidate_progress.items():
            self.add_view(InterviewResultView(progress_key))

bot = MyBot(command_prefix="!", intents=intents)
bot.run(BOT_TOKEN)
