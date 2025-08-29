# cogs/gamble_cog.py
import asyncio
import random
import math
import discord
from discord.ext import commands
from discord.ext.commands import BucketType

from utils.stats import format_num, spend_points, add_points, get_points

MIN_BET = 1000           # 최소 베팅
TICK_SEC = 0.25          # 화면 갱신 간격(초)
GROWTH_PER_TICK = 1.045  # 한 틱마다 배율 * 1.045 (약 4.5% 상승)
MAX_MULTIPLIER = 30.0    # 안전장치: 배율 상한(매우 드문 초고배율 방지)

def roll_crash_point():
    r = random.random()

    if r < 0.08:    # 8% → 1.0x에서 즉시 터짐
        return 1.0
    elif r < 0.50:  # 42% → 1.0~1.5배 
        return round(random.uniform(1.0, 1.5), 2)
    elif r < 0.85:  # 35% → 1.5~3배
        return round(random.uniform(1.5, 3.0), 2)
    elif r < 0.98:  # 13% → 3~10배
        return round(random.uniform(3.0, 10.0), 2)
    else:  # 2% → 10~30배 (가끔 대박)
        return round(random.uniform(10.0, 30.0), 2)

class GambleCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_users: set[int] = set()  # 유저별 동시 진행 방지

    @commands.command(name="도박")
    @commands.cooldown(rate=1, per=10, type=BucketType.user)  # 유저당 10초 쿨다운
    async def crash_game(self, ctx: commands.Context, amount: int):
        # ── 유효성 검사 ───────────────────────────────────────────────
        if amount < MIN_BET:
            await ctx.reply(f"최소 베팅 금액은 {format_num(MIN_BET)} P 입니다.", delete_after=5)
            return
        if ctx.author.id in self.active_users:
            await ctx.reply("이미 진행 중인 도박이 있어요. 잠시만요!", delete_after=5)
            return
        if not spend_points(ctx.author.id, amount):
            await ctx.reply("포인트가 부족합니다.", delete_after=5)
            return

        self.active_users.add(ctx.author.id)

        crash_at = roll_crash_point()
        multiplier = 1.00
        cashed_out = False
        cashed_amount = 0

        # ── UI 준비 ─────────────────────────────────────────────────
        class CashOutView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=None)

            @discord.ui.button(label="💸 지금 받기", style=discord.ButtonStyle.success)
            async def cashout(self, interaction: discord.Interaction, button: discord.ui.Button):
                nonlocal cashed_out, cashed_amount, multiplier
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("이 게임은 호출자만 수령할 수 있어요.", ephemeral=True)
                    return
                if cashed_out:
                    await interaction.response.send_message("이미 수령하셨습니다.", ephemeral=True)
                    return

                # 현재 배율로 확정 지급
                cash_multi = round(multiplier, 2)
                gain = int(math.floor(amount * cash_multi))
                add_points(ctx.author.id, gain)
                cashed_out = True
                cashed_amount = gain

                # 버튼 비활성화
                for c in self.children:
                    c.disabled = True

                await interaction.response.send_message(
                    f"✅ {interaction.user.mention} {cash_multi}x 에서 **{format_num(gain)} P** 수령!",
                    ephemeral=True
                )

        view = CashOutView()

        embed = discord.Embed(
            title="🎲 그래프 도박 (Crash)",
            description=(
                f"베팅: **{format_num(amount)} P**\n"
                f"버튼을 눌러 **크래시 전에** 수령하세요!\n"
                f"현재 배율: **{multiplier:.2f}x**"
            ),
            color=discord.Color.blurple()
        )
        msg = await ctx.send(embed=embed, view=view)

        # ── 진행 루프 ───────────────────────────────────────────────
        try:
            while multiplier < crash_at and multiplier < MAX_MULTIPLIER and not cashed_out:
                await asyncio.sleep(TICK_SEC)
                multiplier *= GROWTH_PER_TICK
                multiplier = min(multiplier, MAX_MULTIPLIER)
                embed.set_field_at if embed.fields else None  # no-op, 안전

                embed = discord.Embed(
                    title="🎲 그래프 도박 (Crash)",
                    description=(
                        f"베팅: **{format_num(amount)} P**\n"
                        f"현재 배율: **{multiplier:.2f}x**\n"
                        f"수령은 **크래시 전**에!"
                    ),
                    color=discord.Color.blurple()
                )
                await msg.edit(embed=embed, view=view)

            # ── 종료 처리 ───────────────────────────────────────────
            for c in view.children:
                c.disabled = True

            if cashed_out:
                # 이미 지급 완료
                after = get_points(ctx.author.id)
                end = discord.Embed(
                    title="🏁 결과",
                    description=(
                        f"수령 성공! **{format_num(cashed_amount)} P** 획득\n"
                        f"최종 배율: **{min(multiplier, crash_at):.2f}x**\n"
                        f"현재 보유: **{format_num(after)} P**"
                    ),
                    color=discord.Color.green()
                )
                await msg.edit(embed=end, view=view)

            else:
                # 크래시(폭파)
                end = discord.Embed(
                    title="💥 CRASHED!",
                    description=(
                        f"크래시 지점: **{crash_at:.2f}x**\n"
                        f"아쉽지만 베팅 {format_num(amount)} P 를 잃었습니다…"
                    ),
                    color=discord.Color.red()
                )
                await msg.edit(embed=end, view=view)

        finally:
            self.active_users.discard(ctx.author.id)

async def setup(bot: commands.Bot):
    await bot.add_cog(GambleCog(bot))
