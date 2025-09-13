import discord
from discord.ext import commands
import os, configparser

from utils.stats import MANG_PATH

from cogs.match import MatchCog
from cogs.economy import EconomyCog
from cogs.stats_view import StatsCog
from cogs.fun_cog import FunCog
from cogs.moderation_cog import ModerationCog
from cogs.gamble_cog import GambleCog

# ───── config.ini 로딩 ─────
config = configparser.ConfigParser()
config.read("config.ini", encoding="utf-8")

def _get_id(section: str, key: str) -> int:
    try:
        v = config.get(section, key, fallback="0")
        return int(v) if str(v).isdigit() else 0
    except Exception:
        return 0

TOKEN = os.getenv("DISCORD_TOKEN") or config.get("Settings", "token", fallback="").strip()

# ───── 역할 ID: config.ini에서 로드 ─────
ROLE_IDS = {
    "사서":     _get_id("Roles", "사서"),
    "수석사서": _get_id("Roles", "수석사서"),
    "큐레이터": _get_id("Roles", "큐레이터"),
    "관장":     _get_id("Roles", "관장"),
    "내전":     _get_id("Roles", "내전"),
}

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def setup_hook():
    # 내전/모더/이코노미 등 모두 동일 ROLE_IDS 전달
    await bot.add_cog(MatchCog(bot, role_ids=ROLE_IDS))
    await bot.add_cog(
        EconomyCog(
            bot,
            grant_role_ids={k: v for k, v in ROLE_IDS.items() if k != "내전"},
            curator_role_id=ROLE_IDS.get("큐레이터"),
        )
    )
    await bot.add_cog(StatsCog(bot))
    await bot.add_cog(FunCog(bot))
    await bot.add_cog(ModerationCog(bot, role_ids=ROLE_IDS))
    await bot.add_cog(GambleCog(bot))

@bot.event
async def on_ready():
    print(f"봇 로그인됨: {bot.user}")

if __name__ == "__main__":
    bot.run(TOKEN)
