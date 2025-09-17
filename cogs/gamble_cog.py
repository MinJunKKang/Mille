# cogs/gamble_cog.py
import asyncio
import random
import math
import configparser
from pathlib import Path
import discord
from discord.ext import commands
from discord.ext.commands import BucketType

from utils.stats import format_num, spend_points, add_points, get_points

MIN_BET = 1000            # 최소 베팅

# ===== 그래프(크래시) 전용 설정 =====
TICK_SEC = 0.25           # (그래프) 화면 갱신 간격(초)
GROWTH_PER_TICK = 1.045   # (그래프) 한 틱마다 배율 * 1.045 (약 4.5% 상승)
MAX_MULTIPLIER = 30.0     # (그래프) 배율 상한

# ===== config.ini에서 채널 ID 읽기 =====
_cfg = configparser.ConfigParser()
try:
    _cfg.read("config.ini", encoding="utf-8")
except Exception:
    pass

def _get_id(section: str, key: str) -> int:
    """config.ini에서 정수 ID 읽기 (없거나 잘못되면 0)."""
    try:
        val = _cfg.get(section, key, fallback="0")
        return int(val) if str(val).isdigit() else 0
    except Exception:
        return 0

# 도박장(명령 허용) 채널 / 도박 결과 로그 채널
GAMBLE_CHANNEL_ID     = _get_id("Gamble", "gamble_channel_id")
GAMBLE_LOG_CHANNEL_ID = _get_id("Gamble", "gamble_log_channel_id")

# ===== 그래프 썸네일 이미지 경로 =====
ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
GRAPH_IMG_NAME = "graph.png"        # assets/graph.png 로 넣어두세요
GRAPH_IMG_PATH = ASSETS_DIR / GRAPH_IMG_NAME

import random

def roll_crash_point():
    """
    크래시 지점 샘플링 (요청한 구간/확률 반영, 총합 101% -> 정규화하여 사용)
      0.51~1.00 : 2%
      1.10~1.30 : 38%
      1.31~1.50 : 25%
      1.51~1.75 : 12%
      1.76~2.00 : 7%
      2.01~2.30 : 5%
      2.31~2.50 : 3%
      2.51~3.00 : 2%
      3.01~4.00 : 2%
      4.01~5.00 : 2%
      5.00~10.00: 1.5%
      10.00~15.00: 1%
      16.00~30.00: 0.5%
    """
    buckets = [
        (0.51, 1.00,  2.0),
        (1.10, 1.30, 38.0),
        (1.31, 1.50, 25.0),
        (1.51, 1.75, 12.0),
        (1.76, 2.00,  7.0),
        (2.01, 2.30,  5.0),
        (2.31, 2.50,  3.0),
        (2.51, 3.00,  2.0),
        (3.01, 4.00,  2.0),
        (4.01, 5.00,  2.0),
        (5.00,10.00,  1.5),
        (10.00,15.00, 1.0),
        (16.00,30.00, 0.5),
    ]
    total = sum(w for _, _, w in buckets)  # 101.0
    # 정규화된 가중치로 1회 샘플
    pick = random.random() * total
    acc = 0.0
    for lo, hi, w in buckets:
        acc += w
        if pick < acc:
            return round(random.uniform(lo, hi), 2)
    # 이론상 도달 X, 안전망
    lo, hi, _ = buckets[-1]
    return round(random.uniform(lo, hi), 2)


class GambleCog(commands.Cog):
    """버튼 도박: !도박1, 그래프 도박: !도박2, 가위바위보 도박: !도박3"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_mines_users: set[int] = set()   # 버튼 도박 동시 진행 방지
        self.active_crash_users: set[int] = set()   # 그래프 도박 동시 진행 방지
        self.active_rps_users: set[int] = set()     # RPS 도박 동시 진행 방지

    # ───────────────── 공지/채널 유틸 ─────────────────
    def _get_log_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """로그 채널이 있으면 우선, 아니면 봇이 글을 보낼 수 있는 첫 텍스트 채널."""
        if GAMBLE_LOG_CHANNEL_ID:
            ch = guild.get_channel(GAMBLE_LOG_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
                return ch
        for c in guild.text_channels:
            if c.permissions_for(guild.me).send_messages:
                return c
        return None

    async def _send_gamble_log(self, guild: discord.Guild | None, *, title: str, description: str, color: int):
        if guild is None:
            return
        ch = self._get_log_channel(guild)
        if not ch:
            return
        try:
            embed = discord.Embed(title=title, description=description, color=color)
            await ch.send(embed=embed)
        except Exception:
            pass

    def _check_gamble_channel(self, ctx: commands.Context) -> bool:
        """도박 명령 사용 가능 채널인지 확인. (설정 없으면 제한 없음)"""
        if not ctx.guild or GAMBLE_CHANNEL_ID == 0:
            return True
        return ctx.channel.id == GAMBLE_CHANNEL_ID

    def _allowed_mention(self) -> str:
        return f"<#{GAMBLE_CHANNEL_ID}>" if GAMBLE_CHANNEL_ID else "도박장(관리자 설정 필요)"
        
    # =================================================================
    # = !도박1 버튼 도박 (4x4, 폭탄6, 배율10 고정 분배, 결과 시 전칸 공개) =
    # = 곱연산 → 합연산 (수령액 = 베팅 * 합산배율)                         =
    # =================================================================
    @commands.command(name="도박1")
    @commands.cooldown(rate=1, per=7, type=BucketType.user)  # 유저당 7초 쿨다운
    async def mines_game(self, ctx: commands.Context, amount: int):
        """
        버튼 도박(마인류)
        - 4x4 격자(16칸): 폭탄 6개 + 배율칸 10개
        - 배율칸의 배당은 고정 목록을 무작위 배치:
        [0.5, 0.5, 0.6, 0.6, 0.7, 0.8, 0.9, 1.0, 1.5, 2.0]
        - '합연산' 방식:
        첫 0.5 → 0.5배, 또 0.5 → 1.0배, 이후 2.0 → 3.0배 ...
        수령 시 지급 = floor(베팅 * (지금까지의 합산 배율))
        - 결과가 나오면 모든 칸 공개 + 로그 채널 공지
        """
        # 채널 제한
        if not self._check_gamble_channel(ctx):
            await ctx.reply(f"이 명령은 {self._allowed_mention()} 에서만 사용할 수 있어요.", delete_after=5)
            return

        if amount < MIN_BET:
            await ctx.reply(f"최소 베팅 금액은 {format_num(MIN_BET)} P 입니다.", delete_after=5)
            return
        if ctx.author.id in self.active_mines_users:
            await ctx.reply("이미 진행 중인 버튼 도박이 있어요. 잠시만요!", delete_after=5)
            return
        if not spend_points(ctx.author.id, amount):
            await ctx.reply("포인트가 부족합니다.", delete_after=5)
            return

        self.active_mines_users.add(ctx.author.id)

        # ----- 보드 구성: 4x4 / 폭탄 6 / 배율 10 -----
        ROWS, COLS = 4, 4
        NCELLS = ROWS * COLS
        NUM_BOMBS = 6

        bomb_positions = set(random.sample(range(NCELLS), NUM_BOMBS))

        MULTIPLIER_POOL = [0.5, 0.5, 0.6, 0.6, 0.7, 0.8, 0.9, 1.0, 1.5, 2.0]
        random.shuffle(MULTIPLIER_POOL)
        safe_cells = [i for i in range(NCELLS) if i not in bomb_positions]
        assert len(safe_cells) == len(MULTIPLIER_POOL), "보드/폭탄/배율 개수 불일치"

        mult_values: dict[int, float] = {
            idx: MULTIPLIER_POOL[pos] for idx, pos in zip(safe_cells, range(len(MULTIPLIER_POOL)))
        }

        revealed: set[int] = set()
        ended = False
        cashed = False
        sum_multiplier = 0.00  # 합연산 누적 배율(초기 0.0)

        # 배율 표시: 둘째 자리 0 제거, 최소 한 자리 유지 (예: 2 → 2.0)
        def fmt1(x: float) -> str:
            s = f"{x:.2f}".rstrip("0").rstrip(".")
            if "." not in s:
                s += ".0"
            return s

        def build_embed(title: str | None = None, crashed: bool = False):
            if title is None:
                title = "🧨 버튼 도박"
            expected = int(math.floor(amount * sum_multiplier))
            desc = [
                f"베팅: **{format_num(amount)} P**",
                f"현재 합산 배율: **{fmt1(sum_multiplier)}x**",
                f"예상 수령: **{format_num(expected)} P**"
            ]
            color = discord.Color.green() if not crashed else discord.Color.red()
            return discord.Embed(title=title, description="\n".join(desc), color=color)

        view_message: discord.Message | None = None
        outer_self = self

        # ----- 결과 시 전칸 공개 헬퍼 -----
        def reveal_all_buttons(view: discord.ui.View):
            for item in view.children:
                if isinstance(item, discord.ui.Button) and hasattr(item, "idx"):
                    idx = getattr(item, "idx")
                    if idx in bomb_positions:
                        item.style = discord.ButtonStyle.danger
                        item.emoji = "💣"
                        item.label = ""
                    else:
                        m = mult_values[idx]
                        item.style = (
                            discord.ButtonStyle.success if idx in revealed else discord.ButtonStyle.secondary
                        )
                        item.emoji = None
                        item.label = f"x{fmt1(m)}"
                    item.disabled = True
                else:
                    if isinstance(item, discord.ui.Button):
                        item.disabled = True

        class CellButton(discord.ui.Button):
            def __init__(self, idx: int, *, row: int):
                super().__init__(label="?", style=discord.ButtonStyle.secondary, row=row)
                self.idx = idx

            async def callback(self, interaction: discord.Interaction):
                nonlocal ended, cashed, sum_multiplier
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("이 게임은 호출자만 누를 수 있어요.", ephemeral=True)
                    return
                if ended or cashed:
                    await interaction.response.send_message("이미 종료된 게임입니다.", ephemeral=True)
                    return
                if self.idx in revealed:
                    await interaction.response.send_message("이미 열린 칸입니다.", ephemeral=True)
                    return

                revealed.add(self.idx)

                if self.idx in bomb_positions:
                    # 폭탄 → 종료 + 전칸 공개 + 로그
                    ended = True
                    self.style = discord.ButtonStyle.danger
                    self.emoji = "💣"
                    self.label = ""
                    self.disabled = True

                    reveal_all_buttons(view)
                    end_embed = discord.Embed(
                        title="💥 폭탄 발동! 게임 종료",
                        description=(f"😵 {interaction.user.mention} 님이 폭탄을 열었습니다!\n"
                                    f"베팅 **{format_num(amount)} P** 를 잃었습니다.\n"
                                    f"진행 중 합산 배율: **{fmt1(sum_multiplier)}x**"),
                        color=discord.Color.red(),
                    )
                    await interaction.response.edit_message(embed=end_embed, view=view)

                    await outer_self._send_gamble_log(
                        interaction.guild,
                        title="🎰 도박 로그 - 버튼(폭탄)",
                        description=(f"{interaction.user.mention} 베팅 **{format_num(amount)} P** "
                                    f"→ **-{format_num(amount)} P** 손실 (합산 **{fmt1(sum_multiplier)}x**)"),
                        color=discord.Color.red().value
                    )
                    view.stop()
                    return

                # 안전 칸 → '합연산' 반영
                m = mult_values[self.idx]
                sum_multiplier = round(sum_multiplier + m, 4)
                self.style = discord.ButtonStyle.success
                self.label = f"x{fmt1(m)}"
                self.disabled = True
                await interaction.response.edit_message(embed=build_embed(), view=view)

        class CashOutButton(discord.ui.Button):
            def __init__(self):
                super().__init__(label="💸 수령", style=discord.ButtonStyle.success, row=ROWS)

            async def callback(self, interaction: discord.Interaction):
                nonlocal ended, cashed, sum_multiplier
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("이 게임은 호출자만 수령할 수 있어요.", ephemeral=True)
                    return
                if ended or cashed:
                    await interaction.response.send_message("이미 종료된 게임입니다.", ephemeral=True)
                    return

                cashed = True
                payout = int(math.floor(amount * sum_multiplier))  # 합연산 결과로 지급
                add_points(ctx.author.id, payout)

                reveal_all_buttons(view)

                done = discord.Embed(
                    title="🏁 수령 완료",
                    description=(f"합산 배율 **{fmt1(sum_multiplier)}x** → **{format_num(payout)} P** 지급!\n"
                                f"현재 보유: **{format_num(get_points(ctx.author.id))} P**"),
                    color=discord.Color.blurple(),
                )
                try:
                    await interaction.response.edit_message(embed=done, view=view)
                finally:
                    net = payout - amount
                    sign = "+" if net >= 0 else "-"
                    await outer_self._send_gamble_log(
                        interaction.guild,
                        title="🎰 도박 로그 - 버튼(수령)",
                        description=(f"{interaction.user.mention} 베팅 **{format_num(amount)} P** "
                                    f"→ 수령 **{format_num(payout)} P** (**{sign}{format_num(abs(net))} P**) "
                                    f"(합산 **{fmt1(sum_multiplier)}x**)"),
                        color=discord.Color.gold().value
                    )
                    view.stop()

        class MinesView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)  # 2분 제한
                for i in range(NCELLS):
                    row = i // COLS
                    self.add_item(CellButton(i, row=row))
                self.add_item(CashOutButton())

            async def on_timeout(self):
                nonlocal ended, cashed, sum_multiplier
                if ended or cashed:
                    self.stop()
                    return
                ended = True
                reveal_all_buttons(self)
                to = discord.Embed(
                    title="⏱️ 시간 초과로 종료",
                    description=(f"선택 시간이 초과되어 베팅 {format_num(amount)} P 를 잃었습니다.\n"
                                f"진행 중 합산 배율: **{fmt1(sum_multiplier)}x**"),
                    color=discord.Color.dark_grey(),
                )
                try:
                    if view_message:
                        await view_message.edit(embed=to, view=self)
                finally:
                    await outer_self._send_gamble_log(
                        view_message.guild if view_message else None,
                        title="🎰 도박 로그 - 버튼(시간초과)",
                        description=(f"{ctx.author.mention} 베팅 **{format_num(amount)} P** "
                                    f"→ **-{format_num(amount)} P** 손실 (합산 **{fmt1(sum_multiplier)}x**)"),
                        color=discord.Color.dark_grey().value
                    )
                    self.stop()

        view = MinesView()
        msg = await ctx.send(embed=build_embed(), view=view)
        view_message = msg

        async def cleanup():
            try:
                await view.wait()
            finally:
                self.active_mines_users.discard(ctx.author.id)

        self.bot.loop.create_task(cleanup())

    # =================================================================
    # =                          !도박2  그래프                        =
    # =================================================================
    @commands.command(name="도박2")
    @commands.cooldown(rate=1, per=10, type=BucketType.user)  # 유저당 10초 쿨다운
    async def crash_game(self, ctx: commands.Context, amount: int):
        if not self._check_gamble_channel(ctx):
            await ctx.reply(f"이 명령은 {self._allowed_mention()} 에서만 사용할 수 있어요.", delete_after=5)
            return

        if amount < MIN_BET:
            await ctx.reply(f"최소 베팅 금액은 {format_num(MIN_BET)} P 입니다.", delete_after=5)
            return
        if ctx.author.id in self.active_crash_users:
            await ctx.reply("이미 진행 중인 그래프 도박이 있어요. 잠시만요!", delete_after=5)
            return
        if not spend_points(ctx.author.id, amount):
            await ctx.reply("포인트가 부족합니다.", delete_after=5)
            return

        self.active_crash_users.add(ctx.author.id)
        outer_self = self

        crash_at = roll_crash_point()
        multiplier = 0.50
        cashed_out = False
        cashed_amount = 0

        # ── 썸네일 파일 준비 (첫 메시지에만 첨부) ──
        thumb_file: discord.File | None = None
        if GRAPH_IMG_PATH.is_file():
            thumb_file = discord.File(GRAPH_IMG_PATH, filename=GRAPH_IMG_NAME)

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
                cash_multi = round(multiplier, 2)
                gain = int(math.floor(amount * cash_multi))
                add_points(ctx.author.id, gain)
                cashed_out = True
                cashed_amount = gain
                for c in self.children:
                    c.disabled = True
                await interaction.response.send_message(
                    f"✅ {interaction.user.mention} {cash_multi}x 에서 **{format_num(gain)} P** 수령!",
                    ephemeral=True
                )
                net = gain - amount
                sign = "+" if net >= 0 else "-"
                await outer_self._send_gamble_log(
                    interaction.guild,
                    title="🎰 도박 로그 - 그래프(수령)",
                    description=(f"{interaction.user.mention} 베팅 **{format_num(amount)} P** "
                                 f"→ 수령 **{format_num(gain)} P** (**{sign}{format_num(abs(net))} P**) "
                                 f"최종 **{cash_multi}x**"),
                    color=discord.Color.gold().value
                )

        view = CashOutView()
        embed = discord.Embed(
            title="🎲 그래프 도박 (Crash)",
            description=(f"베팅: **{format_num(amount)} P**\n"
                         f"버튼을 눌러 **크래시 전에** 수령하세요!\n"
                         f"현재 배율: **{multiplier:.2f}x**"),
            color=discord.Color.blurple()
        )
        if thumb_file:  # 메시지에 첨부될 파일을 가리키는 썸네일
            embed.set_thumbnail(url=f"attachment://{GRAPH_IMG_NAME}")

        # 첫 전송: 파일을 함께 첨부
        msg = await ctx.send(embed=embed, view=view, file=thumb_file)

        try:
            while multiplier < crash_at and multiplier < MAX_MULTIPLIER and not cashed_out:
                await asyncio.sleep(TICK_SEC)
                multiplier *= GROWTH_PER_TICK
                multiplier = min(multiplier, MAX_MULTIPLIER)
                embed = discord.Embed(
                    title="🎲 그래프 도박 (Crash)",
                    description=(f"베팅: **{format_num(amount)} P**\n"
                                 f"현재 배율: **{multiplier:.2f}x**\n"
                                 f"수령은 **크래시 전**에!"),
                    color=discord.Color.blurple()
                )
                if thumb_file:
                    # 이후 편집에서는 같은 메시지의 attachment를 계속 참조
                    embed.set_thumbnail(url=f"attachment://{GRAPH_IMG_NAME}")
                await msg.edit(embed=embed, view=view)

            for c in view.children:
                c.disabled = True

            if cashed_out:
                after = get_points(ctx.author.id)
                end = discord.Embed(
                    title="🏁 결과",
                    description=(f"수령 성공! **{format_num(cashed_amount)} P** 획득\n"
                                 f"최종 배율: **{min(multiplier, crash_at):.2f}x**\n"
                                 f"현재 보유: **{format_num(after)} P**"),
                    color=discord.Color.green()
                )
                if thumb_file:
                    end.set_thumbnail(url=f"attachment://{GRAPH_IMG_NAME}")
                await msg.edit(embed=end, view=view)
            else:
                end = discord.Embed(
                    title="💥 CRASHED!",
                    description=(f"크래시 지점: **{crash_at:.2f}x**\n"
                                 f"아쉽지만 베팅 {format_num(amount)} P 를 잃었습니다…"),
                    color=discord.Color.red()
                )
                if thumb_file:
                    end.set_thumbnail(url=f"attachment://{GRAPH_IMG_NAME}")
                await msg.edit(embed=end, view=view)
                await outer_self._send_gamble_log(
                    ctx.guild,
                    title="🎰 도박 로그 - 그래프(폭파)",
                    description=(f"{ctx.author.mention} 베팅 **{format_num(amount)} P** → **-{format_num(amount)} P** 손실 "
                                 f"(지점 **{crash_at:.2f}x**)"),
                    color=discord.Color.red().value
                )
        finally:
            self.active_crash_users.discard(ctx.author.id)

    # =================================================================
    # =                      !도박3  가위바위보                         =
    # =================================================================
    @commands.command(name="도박3")
    @commands.cooldown(rate=1, per=5, type=BucketType.user)  # 유저당 5초 쿨다운
    async def rps_game(self, ctx: commands.Context, amount: int):
        if not self._check_gamble_channel(ctx):
            await ctx.reply(f"이 명령은 {self._allowed_mention()} 에서만 사용할 수 있어요.", delete_after=5)
            return

        if amount < MIN_BET:
            await ctx.reply(f"최소 베팅 금액은 {format_num(MIN_BET)} P 입니다.", delete_after=5)
            return
        if ctx.author.id in self.active_rps_users:
            await ctx.reply("이미 진행 중인 RPS 도박이 있어요. 잠시만요!", delete_after=5)
            return
        if not spend_points(ctx.author.id, amount):
            await ctx.reply("포인트가 부족합니다.", delete_after=5)
            return

        self.active_rps_users.add(ctx.author.id)

        user_resolved = False
        choices = ["가위", "바위", "보"]
        emojis = {"가위": "✌️", "바위": "✊", "보": "✋"}
        outer_self = self

        desc = (f"베팅: **{format_num(amount)} P**\n"
                f"아래 버튼에서 선택하세요! (승: **1.10x~2.00x 랜덤**, 비김: **멘징**, 패배: **소실**)\n"
                f"시간 제한: 15초")
        embed = discord.Embed(title="🎮 가위바위보 도박", description=desc, color=discord.Color.green())

        class RPSView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=15)
                self.message: discord.Message | None = None

            async def on_timeout(self):
                nonlocal user_resolved
                if user_resolved:
                    return
                add_points(ctx.author.id, amount)  # 본전 환불
                for c in self.children:
                    c.disabled = True
                try:
                    if self.message:
                        to = discord.Embed(
                            title="⌛ 시간 초과",
                            description=f"선택 시간이 초과되어 **{format_num(amount)} P** 가 반환되었습니다.",
                            color=discord.Color.orange()
                        )
                        await self.message.edit(embed=to, view=self)
                except Exception:
                    pass

            async def _handle_choice(self, interaction: discord.Interaction, user_choice: str):
                nonlocal user_resolved
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message("이 게임은 호출자만 선택할 수 있어요.", ephemeral=True)
                    return
                if user_resolved:
                    await interaction.response.send_message("이미 결과가 결정되었습니다.", ephemeral=True)
                    return

                bot_choice = random.choice(choices)
                wins = {"가위": "보", "바위": "가위", "보": "바위"}

                if bot_choice == user_choice:
                    add_points(ctx.author.id, amount)
                    result_title = "🤝 비겼습니다 (멘징)"
                    result_desc = (f"당신: {emojis[user_choice]} **{user_choice}** vs "
                                   f"봇: {emojis[bot_choice]} **{bot_choice}**\n"
                                   f"본전 **{format_num(amount)} P** 반환되었습니다.")
                    color = discord.Color.greyple()
                    await outer_self._send_gamble_log(
                        interaction.guild,
                        title="🎰 도박 로그 - 가위바위보(비김)",
                        description=(f"{interaction.user.mention} 베팅 **{format_num(amount)} P** → 손익 **±0 P**"),
                        color=discord.Color.greyple().value
                    )

                elif wins[user_choice] == bot_choice:
                    multi = round(random.uniform(1.10, 2.00), 2)
                    payout = int(math.floor(amount * multi))
                    add_points(ctx.author.id, payout)
                    result_title = "🏆 승리!"
                    result_desc = (f"당신: {emojis[user_choice]} **{user_choice}** vs "
                                   f"봇: {emojis[bot_choice]} **{bot_choice}**\n"
                                   f"배당 **{multi}x** → **{format_num(payout)} P** 지급!")
                    color = discord.Color.gold()
                    net = payout - amount
                    sign = "+" if net >= 0 else "-"
                    await outer_self._send_gamble_log(
                        interaction.guild,
                        title="🎰 도박 로그 - 가위바위보(승리)",
                        description=(f"{interaction.user.mention} 베팅 **{format_num(amount)} P** "
                                     f"→ 수령 **{format_num(payout)} P** (**{sign}{format_num(abs(net))} P**), "
                                     f"배율 **{multi}x**"),
                        color=discord.Color.gold().value
                    )

                else:
                    result_title = "💣 패배…"
                    result_desc = (f"당신: {emojis[user_choice]} **{user_choice}** vs "
                                   f"봇: {emojis[bot_choice]} **{bot_choice}**\n"
                                   f"베팅 {format_num(amount)} P 를 잃었습니다.")
                    color = discord.Color.red()
                    await outer_self._send_gamble_log(
                        interaction.guild,
                        title="🎰 도박 로그 - 가위바위보(패배)",
                        description=(f"{interaction.user.mention} 베팅 **{format_num(amount)} P** "
                                     f"→ **-{format_num(amount)} P** 손실"),
                        color=discord.Color.red().value
                    )

                user_resolved = True
                for c in self.children:
                    c.disabled = True

                result = discord.Embed(title=result_title, description=result_desc, color=color)
                try:
                    await interaction.response.edit_message(embed=result, view=self)
                except discord.InteractionResponded:
                    if self.message:
                        await self.message.edit(embed=result, view=self)

            @discord.ui.button(label="가위", style=discord.ButtonStyle.primary, emoji="✌️")
            async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button):
                await self._handle_choice(interaction, "가위")

            @discord.ui.button(label="바위", style=discord.ButtonStyle.primary, emoji="✊")
            async def rock(self, interaction: discord.Interaction, button: discord.ui.Button):
                await self._handle_choice(interaction, "바위")

            @discord.ui.button(label="보", style=discord.ButtonStyle.primary, emoji="✋")
            async def paper(self, interaction: discord.Interaction, button: discord.ui.Button):
                await self._handle_choice(interaction, "보")

        view = RPSView()
        msg = await ctx.send(embed=embed, view=view)
        view.message = msg

        async def cleanup():
            try:
                await view.wait()
            finally:
                self.active_rps_users.discard(ctx.author.id)

        self.bot.loop.create_task(cleanup())


async def setup(bot: commands.Bot):
    await bot.add_cog(GambleCog(bot))
