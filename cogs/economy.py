# cogs/economy.py
import discord
from discord.ext import commands
from datetime import datetime
from zoneinfo import ZoneInfo

from utils.stats import (
    load_stats, save_stats, ensure_user, format_num,
)

DAILY_ATTEND_REWARD = 1500

class EconomyCog(commands.Cog):
    """포인트/출석/지갑"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="출석")
    async def attend(self, ctx: commands.Context):
        stats = load_stats()
        uid = str(ctx.author.id)
        rec = ensure_user(stats, uid)

        today_kst = datetime.now(ZoneInfo("Asia/Seoul")).date()
        today_str = today_kst.isoformat()
        last = rec.get("출석_마지막")

        if last == today_str:
            embed = discord.Embed(
                title="출석 체크",
                description="오늘은 이미 출석하셨습니다. 내일 다시 시도해 주세요!",
                color=discord.Color.orange()
            )
            await ctx.send(embed=embed)
            return

        # 지급 & 기록
        rec["포인트"] = int(rec.get("포인트", 0)) + DAILY_ATTEND_REWARD
        rec["출석_마지막"] = today_str
        save_stats(stats)

        embed = discord.Embed(
            title="출석 완료!",
            description=f"{ctx.author.mention} 님, 오늘자 출석 보상으로 **{format_num(DAILY_ATTEND_REWARD)} P** 를 획득했습니다.",
            color=discord.Color.green()
        )
        embed.add_field(name="현재 포인트", value=f"{format_num(rec['포인트'])} P", inline=True)
        embed.set_footer(text="하루 1회 출석 가능")
        await ctx.send(embed=embed)

    @commands.command(name="지갑")
    async def wallet(self, ctx: commands.Context, member: discord.Member | None = None):
        target = member or ctx.author
        stats = load_stats()
        rec = ensure_user(stats, str(target.id))

        points = rec.get("포인트", 0)
        xp = rec.get("경험치", 0)

        embed = discord.Embed(title=f"{target.display_name}님의 정보", color=0x2F3136)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="포인트", value=f"{format_num(points)} P", inline=True)
        embed.add_field(name="경험치", value=f"{format_num(xp)} XP", inline=True)

        await ctx.send(embed=embed)

async def setup(bot: commands.Bot):
    # (옵션) 확장식 로더용. setup_hook에서 add_cog를 쓰는 방식이면 없어도 됨.
    await bot.add_cog(EconomyCog(bot))