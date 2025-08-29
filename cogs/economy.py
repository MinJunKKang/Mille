# cogs/economy.py
import discord
from discord.ext import commands
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Optional, Set

from utils.stats import (
    load_stats, save_stats, ensure_user, format_num,
    spend_points, get_points,
)

DAILY_ATTEND_REWARD = 1500

class EconomyCog(commands.Cog):
    """포인트/출석/지갑/지급/회수"""

    def __init__(self, bot: commands.Bot, grant_role_ids: Optional[Dict[str, int]] = None):
        self.bot = bot
        # !지급 권한이 있는 역할들(ID 집합)
        self.grant_role_ids: Set[int] = set(grant_role_ids.values()) if grant_role_ids else set()

    def _has_grant_power(self, member: discord.Member) -> bool:
        role_ids = {r.id for r in member.roles}
        # 역할 보유자 또는 서버 관리자면 허용
        return bool(role_ids & self.grant_role_ids) or member.guild_permissions.administrator

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

    @commands.command(name="지급")
    async def grant_points(self, ctx: commands.Context, member: discord.Member, amount: int):
        # 권한 검사
        if not self._has_grant_power(ctx.author):
            await ctx.reply("이 명령어를 사용할 권한이 없습니다.", delete_after=5)
            return

        # 금액 유효성
        if amount <= 0:
            await ctx.reply("지급 금액은 1 이상이어야 합니다.", delete_after=5)
            return

        # 자기 자신에게 지급 허용 여부(원하면 막아도 됨)
        #if member.id == ctx.author.id:
        #    await ctx.reply("자기 자신에게는 지급할 수 없습니다.", delete_after=5)
        #    return

        # 지급 처리
        stats = load_stats()
        rec = ensure_user(stats, str(member.id))
        before = int(rec.get("포인트", 0))
        rec["포인트"] = before + amount
        save_stats(stats)

        embed = discord.Embed(
            title="포인트 지급 완료",
            description=(
                f"{member.mention} 님에게 **{format_num(amount)} P** 지급되었습니다.\n"
                f"현재 보유 포인트: **{format_num(rec['포인트'])} P**"
            ),
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"지급자: {ctx.author.display_name}")
        await ctx.send(embed=embed)

    @commands.command(name="회수")
    async def revoke_points(self, ctx: commands.Context, member: discord.Member, amount: int):
        if not self._has_grant_power(ctx.author):
            await ctx.reply("이 명령어를 사용할 권한이 없습니다.", delete_after=5)
            return
        if amount <= 0:
            await ctx.reply("회수 금액은 1 이상이어야 합니다.", delete_after=5)
            return

        if not spend_points(member.id, amount):
            await ctx.send(f"❌ {member.mention} 님은 {format_num(amount)} P를 회수하기에 포인트가 부족합니다.")
            return

        current_points = get_points(member.id)
        embed = discord.Embed(
            title="포인트 회수 완료",
            description=(
                f"{member.mention} 님에게서 **{format_num(amount)} P** 회수했습니다.\n"
                f"현재 보유 포인트: **{format_num(current_points)} P**"
            ),
            color=discord.Color.red()
        )
        embed.set_footer(text=f"회수자: {ctx.author.display_name}")
        await ctx.send(embed=embed)

async def setup(bot: commands.Bot):
    # (옵션) 확장식 로더용. setup_hook에서 add_cog를 쓰는 방식이면 없어도 됨.
    await bot.add_cog(EconomyCog(bot))