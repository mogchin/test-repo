import discord
from discord.ext import commands
import aiosqlite
import os
from dotenv import load_dotenv
from datetime import datetime
import asyncio

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')

# 除外したいボイスチャンネルが含まれるカテゴリのIDを指定してください
EXCLUDED_CATEGORY_ID = 1305735985539055667

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# グローバルなロックを用意
db_lock = asyncio.Lock()


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')

    async with aiosqlite.connect('user_data.db') as db:
        # usersテーブルの作成 (存在しない場合のみ)
        await db.execute('''
                         CREATE TABLE IF NOT EXISTS users
                         (
                             user_id
                             TEXT
                             PRIMARY
                             KEY,
                             joined_at
                             TEXT
                         )
                         ''')

        # pairsテーブルの作成 (存在しない場合のみ、元のカラム構成で)
        await db.execute('''
                         CREATE TABLE IF NOT EXISTS pairs
                         (
                             user1_id
                             TEXT,
                             user2_id
                             TEXT,
                             PRIMARY
                             KEY
                         (
                             user1_id,
                             user2_id
                         )
                             )
                         ''')
        await db.commit()  # 先にテーブルが存在することを確定させる

        # === ★ データベースのマイグレーション処理 ===
        # pairsテーブルにlast_seen_atカラムが存在するか確認
        async with db.execute("PRAGMA table_info(pairs)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]

        # もしlast_seen_atカラムがなければ、データを保持したまま追加する
        if 'last_seen_at' not in columns:
            print("Database migration: Adding 'last_seen_at' column to 'pairs' table...")
            try:
                await db.execute('ALTER TABLE pairs ADD COLUMN last_seen_at TEXT')
                await db.commit()
                print("Migration successful.")
            except Exception as e:
                print(f"Error during migration: {e}")
        else:
            print("'last_seen_at' column already exists.")

    print('Database initialized.')
    print('------')


@bot.event
async def on_voice_state_update(member, before, after):
    # ボイスチャンネルへの参加時のみ処理
    if before.channel is None and after.channel is not None:
        voice_channel = after.channel

        # 特定のカテゴリを除外
        if voice_channel.category and voice_channel.category.id == EXCLUDED_CATEGORY_ID:
            return

        user_id = str(member.id)
        guild = member.guild

        async with db_lock:
            async with aiosqlite.connect('user_data.db') as db:
                # ユーザーがDBに存在しない場合、新規登録
                async with db.execute('SELECT 1 FROM users WHERE user_id = ?', (user_id,)) as cursor:
                    if await cursor.fetchone() is None:
                        joined_at = datetime.utcnow().isoformat()
                        await db.execute(
                            'INSERT INTO users (user_id, joined_at) VALUES (?, ?)',
                            (user_id, joined_at)
                        )

                now = datetime.utcnow()

                # チャンネル内の他のメンバーとペアを作成・更新
                for other_member in voice_channel.members:
                    if other_member.id == member.id:
                        continue

                    other_user_id = str(other_member.id)
                    user1, user2 = sorted([user_id, other_user_id])

                    message = None

                    # ペア情報を取得 (last_seen_atも取得)
                    async with db.execute(
                            'SELECT last_seen_at FROM pairs WHERE user1_id = ? AND user2_id = ?',
                            (user1, user2)
                    ) as cursor:
                        pair_row = await cursor.fetchone()

                    if pair_row is None:
                        # Case 1: 本当のはじめましての場合
                        await db.execute(
                            'INSERT INTO pairs (user1_id, user2_id, last_seen_at) VALUES (?, ?, ?)',
                            (user1, user2, now.isoformat())
                        )
                        message = (
                            f'@**{member.display_name}**さんと'
                            f'@**{other_member.display_name}**さんははじめましてです。'
                        )
                    else:
                        # Case 2: 既にペアが存在する場合
                        last_seen_at_str = pair_row[0]

                        # 新機能適用後、初めて会う場合 (last_seen_at が NULL)
                        if last_seen_at_str is None:
                            # 通知はせず、今回の接続日時を記録するだけ。
                            # 次回から「〜日ぶり」の判定が可能になる。
                            pass
                        else:
                            # 2回目以降の遭遇の場合
                            last_seen = datetime.fromisoformat(last_seen_at_str)
                            delta = now - last_seen

                            # 1ヶ月（30日）以上経過していれば通知
                            if delta.days >= 30:
                                message = (
                                    f'@**{member.display_name}**さんと'
                                    f'@**{other_member.display_name}**さんは約{delta.days}日ぶりですね！'
                                )

                        # 最終接続日時を更新する
                        await db.execute(
                            'UPDATE pairs SET last_seen_at = ? WHERE user1_id = ? AND user2_id = ?',
                            (now.isoformat(), user1, user2)
                        )

                    await db.commit()

                    # メッセージが生成されていれば送信
                    if message:
                        text_channel = discord.utils.get(
                            guild.text_channels, name=voice_channel.name
                        )
                        if text_channel:
                            await text_channel.send(message, delete_after=600)
                            print(f'通知を送信: {message}')
                        else:
                            print(
                                f'ボイスチャンネル "{voice_channel.name}" と同名の'
                                'テキストチャンネルが見つかりません。通知を送信しません。'
                            )


@bot.event
async def on_error(event, *args, **kwargs):
    with open('error.log', 'a', encoding='utf-8') as f:
        if event == 'on_voice_state_update':
            f.write(f'Unhandled exception in {event}\n')
            f.write(f'Args: {args}\n')
            f.write(f'Kwargs: {kwargs}\n')
        else:
            raise


bot.run(TOKEN)