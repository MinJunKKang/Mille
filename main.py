# main.py
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

# from 포켓몬8 import subtract_points_from_user, save_user_data, get_user_points, place_bet, process_betting_result

config = configparser.ConfigParser()
config.read("config.ini", encoding="utf-8")
TOKEN = os.getenv("DISCORD_TOKEN") or config.get("Settings", "token", fallback="").strip()

# 역할 ID (내전)
ROLE_IDS = {
    "사서": 1409174707307151418,
    "수석사서": 1409174707307151419,
    "큐레이터": 1409174707307151416,
    "관장": 1409174707315544064,
    "내전": 1409174707315544065,
}

CLEANUP_ROLE_IDS = {k: ROLE_IDS[k] for k in ("사서", "수석사서", "큐레이터", "관장")}
 
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def setup_hook():
    await bot.add_cog(MatchCog(bot, role_ids=ROLE_IDS))
    await bot.add_cog(EconomyCog(bot, grant_role_ids={k:v for k,v in ROLE_IDS.items() if k!="내전"}))
    await bot.add_cog(StatsCog(bot))
    await bot.add_cog(FunCog(bot))
    await bot.add_cog(ModerationCog(bot, role_ids=ROLE_IDS))
    await bot.add_cog(GambleCog(bot))

game_counter = 1
games = {}  
active_hosts = set()  

@bot.event
async def on_ready():
    print(f"봇 로그인됨: {bot.user}")

if __name__ == "__main__":
    bot.run(TOKEN)