# cogs/economy.py
import random
import discord
from discord.ext import commands, tasks
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Set, Dict

from utils.stats import (
    load_stats, save_stats, ensure_user, format_num,
    spend_points, get_points, add_points
)

DAILY_ATTEND_REWARD = 1500

# 1순위: 공지 기본 채널 ID
DEFAULT_VOICE_ANNOUNCE_CHANNEL_ID = 1409174709416759420


class EconomyCog(commands.Cog):
    """포인트/출석/지갑/지급/회수/보이스랜덤(스케줄)"""

    def __init__(self, bot: commands.Bot, grant_role_ids: Optional[Dict[str, int]] = None):
        self.bot = bot
        self.grant_role_ids: Set[int] = set(grant_role_ids.values()) if grant_role_ids else set()

        # 보이스 랜덤 스케줄 상태
        self.voice_grant_enabled: bool = True
        self.voice_grant_amount: int = 1000

        # 스케줄 시작
        self.voice_grant_task.start()

    # --------- 권한/헬퍼 ---------
    def _has_grant_power(self, member: discord.Member) -> bool:
        role_ids = {r.id for r in member.roles}
        return bool(role_ids & self.grant_role_ids) or member.guild_permissions.administrator

    def _pick_voice_candidates(self, guild: discord.Guild):
        """AFK/봇 제외하고 음성/스테이지 채널 참여자 수집"""
        candidates = []
        afk_id = guild.afk_channel.id if guild.afk_channel else None
        voice_like = list(guild.voice_channels) + list(getattr(guild, "stage_channels", []))
        for ch in voice_like:
            if afk_id and ch.id == afk_id:
                continue
            for m in ch.members:
                if not m.bot:
                    candidates.append((m, ch))
        return candidates

    def _get_announce_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """공지 채널 선택:
        1) DEFAULT_VOICE_ANNOUNCE_CHANNEL_ID
        2) 봇이 send_messages 권한이 있는 첫 텍스트 채널
        """
        # 1) 고정 ID 우선
        ch = guild.get_channel(DEFAULT_VOICE_ANNOUNCE_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
            return ch

        # 2) 첫 사용 가능 텍스트 채널
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                return ch
        return None

    # --------- 출석/지갑/지급/회수 ---------
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
        if not self._has_grant_power(ctx.author):
            await ctx.reply("이 명령어를 사용할 권한이 없습니다.", delete_after=5)
            return
        if amount <= 0:
            await ctx.reply("지급 금액은 1 이상이어야 합니다.", delete_after=5)
            return

        stats = load_stats()
        rec = ensure_user(stats, str(member.id))
        rec["포인트"] = int(rec.get("포인트", 0)) + amount
        save_stats(stats)

        embed = discord.Embed(
            title="포인트 지급 완료",
            description=(f"{member.mention} 님에게 **{format_num(amount)} P** 지급되었습니다.\n"
                         f"현재 보유 포인트: **{format_num(rec['포인트'])} P**"),
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
            description=(f"{member.mention} 님에게서 **{format_num(amount)} P** 회수했습니다.\n"
                         f"현재 보유 포인트: **{format_num(current_points)} P**"),
            color=discord.Color.red()
        )
        embed.set_footer(text=f"회수자: {ctx.author.display_name}")
        await ctx.send(embed=embed)
    # --------- 보이스 랜덤: 수동 실행 (관리자 전용) ---------
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    @commands.command(name="보이스랜덤", aliases=["음성추첨", "보이스추첨"])
    async def random_voice_grant(self, ctx: commands.Context, amount: int = 1000):
        """현재 서버의 모든 음성/스테이지 채널 참여자 중 랜덤 1명에게 포인트 지급(수동)."""
        if amount <= 0:
            await ctx.reply("지급 금액은 1 이상이어야 합니다.", delete_after=5)
            return

        guild = ctx.guild
        candidates = self._pick_voice_candidates(guild)
        if not candidates:
            await ctx.send("지금은 어떤 음성 채널에도 사람이 없어요. 😴")
            return

        winner, vch = random.choice(candidates)
        new_balance = add_points(winner.id, amount)

        embed = discord.Embed(
            title="🎉 랜덤 지급",
            description=(f"{vch.mention} 에서 랜덤 추첨!\n"
                         f"당첨자: {winner.mention}\n"
                         f"지급액: **{format_num(amount)} P**\n"
                         f"현재 보유 포인트: **{format_num(new_balance)} P**"),
            color=discord.Color.gold()
        )
        await ctx.send(embed=embed)

    @random_voice_grant.error
    async def _rv_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("이 명령은 **관리자만** 사용할 수 있어요.", delete_after=5)

    # --------- 보이스 랜덤: 30분마다 스케줄 ---------
    @tasks.loop(minutes=30)
    async def voice_grant_task(self):
        """30분마다 각 길드에서 보이스 랜덤 지급"""
        if not self.voice_grant_enabled:
            return

        for guild in list(self.bot.guilds):
            try:
                candidates = self._pick_voice_candidates(guild)
                if not candidates:
                    continue

                winner, vch = random.choice(candidates)
                new_balance = add_points(winner.id, self.voice_grant_amount)

                ch = self._get_announce_channel(guild)
                if not ch:
                    continue

                embed = discord.Embed(
                    title="🎉 랜덤 지급",
                    description=(f"{vch.mention} 에서 랜덤 추첨!\n"
                                 f"당첨자: {winner.mention}\n"
                                 f"지급액: **{format_num(self.voice_grant_amount)} P**\n"
                                 f"현재 보유 포인트: **{format_num(new_balance)} P**"),
                    color=discord.Color.gold()
                )
                await ch.send(embed=embed)
            except Exception:
                continue  # 길드 단위 예외는 넘기고 다음 길드 진행

    @voice_grant_task.before_loop
    async def _before_voice_grant_task(self):
        await self.bot.wait_until_ready()

    # --------- 스케줄 토글/설정 (관리자 전용) ---------
    @commands.has_guild_permissions(administrator=True)
    @commands.command(name="보이스랜덤-온")
    async def voice_random_on(self, ctx: commands.Context):
        self.voice_grant_enabled = True
        await ctx.send("보이스 랜덤 지급 스케줄이 **켜졌습니다**. (30분마다 실행)")

    @voice_random_on.error
    async def _on_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("이 명령은 **관리자만** 사용할 수 있어요.", delete_after=5)

    @commands.has_guild_permissions(administrator=True)
    @commands.command(name="보이스랜덤-오프")
    async def voice_random_off(self, ctx: commands.Context):
        self.voice_grant_enabled = False
        await ctx.send("보이스 랜덤 지급 스케줄이 **꺼졌습니다**.")

    @voice_random_off.error
    async def _off_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("이 명령은 **관리자만** 사용할 수 있어요.", delete_after=5)

    @commands.has_guild_permissions(administrator=True)
    @commands.command(name="보이스랜덤-금액")
    async def voice_random_amount(self, ctx: commands.Context, amount: int):
        if amount <= 0:
            await ctx.reply("금액은 1 이상이어야 합니다.", delete_after=5)
            return
        self.voice_grant_amount = amount
        await ctx.send(f"보이스 랜덤 지급액을 **{format_num(amount)} P** 로 설정했습니다.")

    @voice_random_amount.error
    async def _amount_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("이 명령은 **관리자만** 사용할 수 있어요.", delete_after=5)


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))