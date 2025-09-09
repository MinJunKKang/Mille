# cogs/match.py
import asyncio
import random
import re
import urllib.parse
import json
import discord
from discord.ext import commands
from discord.ui import View, Button, Select, Modal, TextInput
from typing import Dict, Set, List, Optional, Tuple

from utils.stats import update_result_dual, MANG_PATH  # 승/패 기록 반영

# 내전 기록 채널(텍스트 채널) ID
MATCH_LOG_CHANNEL_ID = 1409174709718880329

# ===== 도우미 함수 =====
def create_opgg_multisearch_url(summoner_list: List[str]) -> str:
    base_url = "https://op.gg/ko/lol/multisearch/kr?summoners="
    encoded = [urllib.parse.quote(s) for s in summoner_list]
    return base_url + ",".join(encoded)

def clean_opgg_name(name: str) -> str:
    return re.sub(r"[^\w\s가-힣/#]", "", name).split('/')[0].strip()


# ===== 데이터 구조 =====
class Game:
    def __init__(self, game_id: int, host_id: int, channel_id: int, max_players: int = 10):
        self.id = game_id
        self.host_id = host_id
        self.channel_id = channel_id
        self.max_players = max_players
        self.participants: List[int] = [host_id]
        self.started = False
        self.message: Optional[discord.Message] = None
        self.team_captains: List[int] = []
        self.teams: Dict[int, List[int]] = {1: [], 2: []}
        self.pick_order: List[int] = []
        self.draft_turn = 0
        self.finished = False
        self.result_message: Optional[discord.Message] = None
        self.team_status_message: Optional[discord.Message] = None
        self.bets: Dict[int, Dict[str, int]] = {}
        self.pick_history: List[Tuple[int, int]] = []  # (team_num, user_id)

    def is_full(self) -> bool:
        return len(self.participants) >= self.max_players

    def add_participant(self, user_id: int) -> bool:
        if user_id not in self.participants and not self.is_full():
            self.participants.append(user_id)
            return True
        return False

    def remove_participant(self, user_id: int) -> bool:
        if user_id in self.participants and user_id != self.host_id:
            self.participants.remove(user_id)
            return True
        return False


# ====== Cog ======
class MatchCog(commands.Cog):
    """내전(로비/드래프트/결과 기록/OPGG 버튼) 전담 Cog"""

    def __init__(self, bot: commands.Bot, role_ids: Dict[str, int]):
        self.bot = bot
        self.role_ids = role_ids
        self.game_counter: int = 1
        self.games: Dict[int, Game] = {}
        self.active_hosts: Set[int] = set()

    def _get_match_log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """내전 기록을 보낼 텍스트 채널을 찾는다."""
        ch = guild.get_channel(MATCH_LOG_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:
            return ch
        # 폴백: 봇이 보낼 수 있는 첫 텍스트 채널
        for c in guild.text_channels:
            if c.permissions_for(guild.me).send_messages:
                return c
        return None

    # --------- 내부 유틸 ---------
    async def get_sorted_participants_by_tier(self, guild: discord.Guild, user_ids: List[int]) -> List[str]:
        tier_order = {"C": 0, "GM": 1, "M": 2, "D": 3, "E": 4, "P": 5, "G": 6, "S": 7, "B": 8, "I": 9}
        def parse_tier(text: str):
            match = re.search(r"(C|GM|M|D|E|P|G|S|B|I)(\d+)", text.upper())
            if match:
                tier, num = match.groups()
                num = int(num)
                tier_rank = tier_order.get(tier, 999)
                score = -num if tier in ("C", "GM", "M") else num
                return (tier_rank, score)
            return (999, 999)

        entries = []
        for uid in user_ids:
            member = guild.get_member(uid)
            if not member:
                continue
            name = member.display_name
            entries.append((name, parse_tier(name)))

        sorted_entries = sorted(entries, key=lambda x: x[1])
        return [entry[0] for entry in sorted_entries]

    async def start_team_leader_selection(self, interaction: discord.Interaction, game: Game):
        guild = interaction.guild
        assert guild is not None

        sorted_names = await self.get_sorted_participants_by_tier(guild, game.participants)
        name_to_user = {guild.get_member(uid).display_name: uid for uid in game.participants if guild.get_member(uid)}

        options = []
        for name in sorted_names:
            uid = name_to_user.get(name)
            if uid:
                options.append(discord.SelectOption(label=name, value=str(uid)))

        cog = self

        class CaptainSelectView(View):
            def __init__(self):
                super().__init__(timeout=None)

            @discord.ui.select(placeholder="팀장을 선택하세요 (두 명)", min_values=2, max_values=2, options=options)
            async def select_callback(self, inner_interaction: discord.Interaction, select: Select):
                if inner_interaction.user.id != game.host_id:
                    await inner_interaction.response.send_message("팀장 선택은 개최자만 가능합니다.", ephemeral=True)
                    return

                game.team_captains = [int(uid) for uid in select.values]

                embed = discord.Embed(
                    title="팀장 선택 완료",
                    description="팀장이 선택되었습니다! 팀 구성을 시작합니다.",
                    color=0x2F3136
                )
                await inner_interaction.response.edit_message(embed=embed, view=None)
                await cog.start_draft(inner_interaction, game)

        embed = discord.Embed(
            title="팀장 선택",
            description="티어 순으로 정렬된 명단에서 팀장을 선택해주세요:",
            color=0x2F3136
        )
        await interaction.channel.send(embed=embed, view=CaptainSelectView())

    async def start_draft(self, interaction: discord.Interaction, game: Game):
        players = [uid for uid in game.participants if uid not in game.team_captains]
        random.shuffle(players)
        first = random.choice([1, 2])

        random.shuffle(game.team_captains)
        game.teams[1].append(game.team_captains[0])
        game.teams[2].append(game.team_captains[1])

        game.pick_order = [1, 2, 2, 1, 1, 2, 2, 1] if first == 1 else [2, 1, 1, 2, 2, 1, 1, 2]

        guild = interaction.guild
        assert guild is not None

        c1 = guild.get_member(game.team_captains[0]).display_name
        c2 = guild.get_member(game.team_captains[1]).display_name
        embed = discord.Embed(title=f"내전 #{game.id} 팀 구성 현황", color=0x2F3136)
        embed.add_field(name="1팀", value=f"- {c1}", inline=True)
        embed.add_field(name="2팀", value=f"- {c2}", inline=True)

        game.team_status_message = await interaction.channel.send(embed=embed)
        await self.send_draft_ui(interaction.channel, game, players)

    async def send_draft_ui(self, channel: discord.TextChannel, game: Game, available: List[int]):
        if not available or game.draft_turn >= len(game.pick_order):
            await self.finish_teams(channel, game)
            return

        team_num = game.pick_order[game.draft_turn]
        captain_id = game.team_captains[team_num - 1]
        guild = channel.guild

        def create_team_embed():
            team1_members = [guild.get_member(u).display_name for u in game.teams[1]]
            team2_members = [guild.get_member(u).display_name for u in game.teams[2]]
            embed = discord.Embed(title=f"내전 #{game.id} 팀 구성 현황", color=0x2F3136)
            embed.add_field(name="1팀", value="\n".join(f"- {n}" for n in team1_members) or "-", inline=True)
            embed.add_field(name="2팀", value="\n".join(f"- {n}" for n in team2_members) or "-", inline=True)
            return embed

        cog = self

        class DraftView(View):
            def __init__(self):
                super().__init__(timeout=None)

            @discord.ui.select(
                placeholder=f"{team_num}팀 픽 대상 선택",
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(
                        label=guild.get_member(uid).display_name,
                        value=str(uid)
                    ) for uid in available
                ]
            )
            async def select_callback(self, interaction: discord.Interaction, select: Select):
                if interaction.user.id != captain_id:
                    await interaction.response.send_message("지금은 다른 팀장의 차례입니다.", ephemeral=True)
                    return

                uid = int(select.values[0])
                if uid not in available:
                    await interaction.response.send_message("이미 선택된 유저입니다.", ephemeral=True)
                    return

                # 선택 반영
                game.teams[team_num].append(uid)
                available.remove(uid)

                # 히스토리 기록 (되돌리기용)
                game.pick_history.append((team_num, uid))

                # 턴 진행
                game.draft_turn += 1

                # 팀 현황 갱신 + 현재 선택 UI 삭제 후 다음 UI 띄우기
                await game.team_status_message.edit(embed=create_team_embed())
                await interaction.message.delete()
                await cog.send_draft_ui(channel, game, available)

            @discord.ui.button(label="↩ 되돌리기", style=discord.ButtonStyle.secondary)
            async def undo_pick(self, interaction: discord.Interaction, button: Button):
                # 권한: 개최자 또는 관리자만
                if interaction.user.id != game.host_id and not interaction.user.guild_permissions.manage_guild:
                    await interaction.response.send_message("되돌리기는 개최자 또는 관리자만 가능합니다.", ephemeral=True)
                    return

                if not game.pick_history:
                    await interaction.response.send_message("되돌릴 선택이 없습니다.", ephemeral=True)
                    return

                # 마지막 픽 되돌리기
                last_team, last_uid = game.pick_history.pop()

                # 팀에서 제거
                if last_uid in game.teams[last_team]:
                    game.teams[last_team].remove(last_uid)

                # 다시 선택 가능 목록에 복귀
                if last_uid not in available:
                    available.append(last_uid)

                # 턴 되돌리기
                if game.draft_turn > 0:
                    game.draft_turn -= 1

                # 팀 현황 갱신
                await game.team_status_message.edit(embed=create_team_embed())

                # 현재 선택 UI 교체
                try:
                    await interaction.message.delete()
                except:
                    pass
                await cog.send_draft_ui(channel, game, available)


        embed = discord.Embed(
            title=f"{team_num}팀 팀원 선택",
            description=f"{guild.get_member(captain_id).display_name}님, 팀원을 선택하세요:",
            color=0x2F3136
        )
        await channel.send(embed=embed, view=DraftView())

    async def finish_teams(self, channel: discord.TextChannel, game: Game):
        guild = channel.guild

        team1_members, team2_members = [], []
        team1_opgg_names, team2_opgg_names = [], []

        for uid in game.teams[1]:
            member = guild.get_member(uid)
            nickname = member.display_name if member else "알 수 없음"
            display = f"⭐ {nickname}" if uid == game.team_captains[0] else f"- {nickname}"
            team1_members.append(display)
            if nickname != "알 수 없음":
                team1_opgg_names.append(clean_opgg_name(nickname))

        for uid in game.teams[2]:
            member = guild.get_member(uid)
            nickname = member.display_name if member else "알 수 없음"
            display = f"⭐ {nickname}" if uid == game.team_captains[1] else f"- {nickname}"
            team2_members.append(display)
            if nickname != "알 수 없음":
                team2_opgg_names.append(clean_opgg_name(nickname))

        t1 = "\n".join(team1_members)
        t2 = "\n".join(team2_members)

        opgg1 = create_opgg_multisearch_url(team1_opgg_names)
        opgg2 = create_opgg_multisearch_url(team2_opgg_names)

        embed = discord.Embed(title=f"⚔️ 내전 #{game.id} 팀 구성 완료", color=0x2F3136)
        embed.add_field(name="🟦 1팀", value=t1 or "- 없음", inline=True)
        embed.add_field(name="🟥 2팀", value=t2 or "- 없음", inline=True)
        embed.set_footer(text="전적 보기 버튼은 아래에 있습니다 👇")

        result_view = self.ResultView(self, game)
        result_message = await channel.send(embed=embed, view=result_view)
        game.result_message = result_message

        opgg_view = self.OpggButtonView(opgg1, opgg2)
        await channel.send(view=opgg_view)

        log_ch = self._get_match_log_channel(guild)
        if log_ch:
            host_member = guild.get_member(game.host_id)
            host_name = host_member.display_name if host_member else str(game.host_id)

            log_embed = discord.Embed(
                title=f"⚔️ 내전 #{game.id} 팀 구성 완료",
                description=f"개최자: {host_name}",
                color=0x2F3136
            )
            log_embed.add_field(name="🟦 1팀", value=t1 or "- 없음", inline=True)
            log_embed.add_field(name="🟥 2팀", value=t2 or "- 없음", inline=True)
            log_embed.set_footer(text="아래 버튼으로 전적 확인")

            # 기록방에도 OPGG 버튼 함께 전송
            await log_ch.send(embed=log_embed, view=self.OpggButtonView(opgg1, opgg2))

        asyncio.create_task(self.disable_buttons_after_timeout(result_message, result_view, 10800))
        await channel.send(view=self.BettingView(game))

    async def disable_buttons_after_timeout(self, message: discord.Message, view: View, seconds: int):
        await asyncio.sleep(seconds)

        # 이미 종료된 경우 무시
        if hasattr(view, "game") and getattr(view.game, "finished", False):
            return

        for item in view.children:
            item.disabled = True

        embed = message.embeds[0]
        embed.add_field(name="상태", value="⏱️ 시간 초과로 인해 종료되었습니다.", inline=False)

        try:
            await message.edit(embed=embed, view=view)
        except:
            pass

    # ========= 스크림 =========
    @commands.command(name="스크림", aliases=["스크림전적"])
    async def scrim_stats(self, ctx: commands.Context, member: discord.Member | None = None):
        """스크림 전적 조회 (자신 또는 멘션한 대상)"""
        target = member or ctx.author

        try:
            with open(MANG_PATH, "r", encoding="utf-8") as f:
                stats = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            stats = {}

        rec = stats.get(str(target.id))
        if not rec or rec.get("참여", 0) == 0:
            if target.id == ctx.author.id:
                await ctx.send("❌ 스크림에 참여한 기록이 없습니다.")
            else:
                await ctx.send(f"❌ {target.display_name}님의 스크림 기록이 없습니다.")
            return

        total = rec.get("참여", 0)
        wins  = rec.get("승리", 0)
        losses = total - wins
        winrate = round(wins / total * 100, 2) if total else 0.0

        embed = discord.Embed(
            title=f"🎮 {target.display_name}님의 스크림 전적",
            color=discord.Color.dark_red()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="참여", value=f"{total}전", inline=True)
        embed.add_field(name="승리", value=f"{wins}승", inline=True)
        embed.add_field(name="패배", value=f"{losses}패", inline=True)
        embed.add_field(name="승률", value=f"{winrate}%", inline=False)

        await ctx.send(embed=embed)

    # --------- 명령어: 내전 시작 ---------
    @commands.command(name="내전")
    async def start_match(self, ctx: commands.Context):
        game_id = self.game_counter
        self.game_counter += 1

        game = Game(game_id, ctx.author.id, ctx.channel.id)
        self.games[game_id] = game
        self.active_hosts.add(ctx.author.id)

        participants_list = f"1. {ctx.author.display_name}\n"

        embed = discord.Embed(
            title=f"내전 #{game_id} - {ctx.author.display_name}",
            description=f"인원: 1/{game.max_players}",
            color=0x2F3136
        )
        embed.add_field(name="참여자", value=participants_list, inline=False)

        view = self.LobbyView(self, game)

        # 역할 멘션
        role_id = self.role_ids.get("내전")
        role = ctx.guild.get_role(role_id) if role_id else None
        if role is None:
            role = discord.utils.get(ctx.guild.roles, name="내전")

        allowed = discord.AllowedMentions(roles=[role] if role else [])
        content = role.mention if role else None

        message = await ctx.send(content=content, embed=embed, view=view, allowed_mentions=allowed)
        game.message = message

    # ========= 뷰들 =========
    class LobbyView(View):
        def __init__(self, cog: "MatchCog", game: Game):
            super().__init__(timeout=None)
            self.cog = cog
            self.game = game

        async def update_message(self):
            current = len(self.game.participants)
            guild = self.game.message.guild
            host = guild.get_member(self.game.host_id)

            participants_list = ""
            for idx, user_id in enumerate(self.game.participants, 1):
                member = guild.get_member(user_id)
                if member:
                    participants_list += f"{idx}. {member.display_name}\n"

            embed = discord.Embed(
                title=f"내전 #{self.game.id} - {host.display_name}",
                description=f"인원: {current}/{self.game.max_players}",
                color=0x2F3136
            )
            embed.add_field(name="참여자", value=participants_list or "아직 참여자가 없습니다.", inline=False)

            await self.game.message.edit(content=None, embed=embed, view=self)

        @discord.ui.button(label="참여", style=discord.ButtonStyle.success)
        async def join(self, interaction: discord.Interaction, button: Button):
            try:
                await interaction.response.defer()
            except discord.NotFound:
                return

            user_id = interaction.user.id
            if self.game.add_participant(user_id):
                await self.update_message()

                if self.game.is_full():
                    sorted_list = await self.cog.get_sorted_participants_by_tier(interaction.guild, self.game.participants)
                    embed = discord.Embed(title="📋 티어 기준 정렬된 참여자", color=0x2F3136)
                    embed.description = "\n".join([f"{i+1}. {entry}" for i, entry in enumerate(sorted_list)])
                    await interaction.channel.send(embed=embed)

                    self.clear_items()
                    await self.game.message.edit(view=self.cog.StartEndView(self.cog, self.game))

                    for uid in self.game.participants:
                        member = interaction.guild.get_member(uid)
                        if member:
                            try:
                                await member.send(
                                    f"📢 내전 #{self.game.id} 참가자가 모두 모였습니다!\n"
                                    f"팀장 선택이 곧 시작됩니다. 채널로 돌아와주세요!"
                                )
                            except:
                                pass
            else:
                try:
                    await interaction.followup.send("이미 참여했거나 모집이 마감되었습니다.", ephemeral=True)
                except:
                    pass

        @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: Button):
            user_id = interaction.user.id
            if self.game.remove_participant(user_id):
                await self.update_message()
                await interaction.response.defer()

                log_channel = interaction.guild.get_channel(1367420842350219356)
                if log_channel:
                    member = interaction.user
                    await log_channel.send(
                        f"🚪 `{member.display_name}`님이 내전 #{self.game.id}에서 참여를 취소했습니다."
                    )
            else:
                if user_id == self.game.host_id:
                    await interaction.response.send_message("개최자는 참여를 취소할 수 없습니다.", ephemeral=True)
                else:
                    await interaction.response.send_message("참여 중이 아닙니다.", ephemeral=True)

        @discord.ui.button(label="종료", style=discord.ButtonStyle.danger)
        async def end(self, interaction: discord.Interaction, button: Button):
            if interaction.user.id != self.game.host_id:
                await interaction.response.send_message("이 명령은 개최자만 실행할 수 있습니다.", ephemeral=True)
                return

            embed = discord.Embed(
                title="내전 모집 취소",
                description="내전 모집이 취소되었습니다.",
                color=0x2F3136
            )
            await interaction.response.edit_message(embed=embed, view=None)
            self.cog.games.pop(self.game.id, None)
            self.cog.active_hosts.discard(self.game.host_id)

    class StartEndView(View):
        def __init__(self, cog: "MatchCog", game: Game):
            super().__init__(timeout=None)
            self.cog = cog
            self.game = game
            self.add_item(Button(label="시작", style=discord.ButtonStyle.primary, custom_id="start"))
            self.add_item(Button(label="종료", style=discord.ButtonStyle.danger, custom_id="end"))

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if interaction.data["custom_id"] == "start":
                if interaction.user.id != self.game.host_id:
                    await interaction.response.send_message("게임 시작은 개최자만 가능합니다.", ephemeral=True)
                    return False
                self.game.started = True

                embed = discord.Embed(title="팀장 선택", description="팀장 선택을 시작합니다!", color=0x2F3136)
                await interaction.response.edit_message(embed=embed, view=None)
                await self.cog.start_team_leader_selection(interaction, self.game)
                return True

            elif interaction.data["custom_id"] == "end":
                if interaction.user.id != self.game.host_id:
                    await interaction.response.send_message("이 명령은 개최자만 실행할 수 있습니다.", ephemeral=True)
                    return False

                embed = discord.Embed(title="내전 모집 취소", description="내전 모집이 취소되었습니다.", color=0x2F3136)
                await interaction.response.edit_message(embed=embed, view=None)
                self.cog.games.pop(self.game.id, None)
                self.cog.active_hosts.discard(self.game.host_id)
                return True

            return True

    class OpggButtonView(View):
        def __init__(self, url1: str, url2: str, timeout: int = 10800):
            super().__init__(timeout=timeout)
            self.add_item(discord.ui.Button(label="🔎 1팀 전적 보기", url=url1, style=discord.ButtonStyle.link))
            self.add_item(discord.ui.Button(label="🔎 2팀 전적 보기", url=url2, style=discord.ButtonStyle.link))

    class BettingView(View):
        def __init__(self, game: Game):
            super().__init__(timeout=210)
            self.game = game

        @discord.ui.button(label="1팀에 배팅", style=discord.ButtonStyle.success)
        async def bet_team1(self, interaction: discord.Interaction, button: Button):
            await self.handle_bet(interaction, team=1)

        @discord.ui.button(label="2팀에 배팅", style=discord.ButtonStyle.success)
        async def bet_team2(self, interaction: discord.Interaction, button: Button):
            await self.handle_bet(interaction, team=2)

        async def handle_bet(self, interaction: discord.Interaction, team: int):
            game = self.game

            class BetModal(Modal, title="배팅 금액 입력"):
                amount = TextInput(label="배팅할 금액", placeholder="숫자만 입력 (최소 1000₽)", required=True)

                def __init__(self, game: Game, team: int):
                    super().__init__()
                    self.game = game
                    self.team = team

                async def on_submit(self, modal_interaction: discord.Interaction):
                    user_id = modal_interaction.user.id
                    try:
                        amount_int = int(self.amount.value)
                        if amount_int < 1000:
                            await modal_interaction.response.send_message("❌ 최소 배팅 금액은 1000₽입니다.", ephemeral=True)
                            return
                    except:
                        await modal_interaction.response.send_message("❌ 숫자만 입력해 주세요.", ephemeral=True)
                        return

                    if user_id in self.game.bets:
                        await modal_interaction.response.send_message("❌ 이미 배팅하셨습니다.", ephemeral=True)
                        return

                    self.game.bets[user_id] = {"amount": amount_int, "team": self.team}
                    await modal_interaction.response.send_message(
                        f"✅ {modal_interaction.user.mention}님이 {self.team}팀에 {amount_int}₽ 배팅했습니다.",
                        ephemeral=False
                    )

            await interaction.response.send_modal(BetModal(game, team))

    class ResultView(View):
        def __init__(self, cog: "MatchCog", game: Game):
            super().__init__(timeout=None)
            self.cog = cog
            self.game = game

        @discord.ui.button(label="1팀 승리", style=discord.ButtonStyle.primary)
        async def team1_win(self, interaction: discord.Interaction, button: Button):
            if interaction.user.id != self.game.host_id and not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("개최자 또는 관리자만 결과를 기록할 수 있습니다.", ephemeral=True)
                return
            if self.game.finished:
                await interaction.response.send_message("이미 결과가 기록되었습니다.", ephemeral=True)
                return

            uids_team1 = list(set([self.game.team_captains[0]] + self.game.teams[1]))
            uids_team2 = list(set([self.game.team_captains[1]] + self.game.teams[2]))

            for uid in uids_team1:
                update_result_dual(str(uid), True)
            for uid in uids_team2:
                update_result_dual(str(uid), False)

            self.game.finished = True
            self.team1_win.disabled = True
            self.team2_win.disabled = True
            self.cancel_game.disabled = True
            self.add_item(MatchCog.PlayAgainButton(self.cog, self.game))
            self.add_item(MatchCog.RevengeButton(self.cog, self.game))
            self.add_item(MatchCog.EndGameButton(self.cog, self.game))

            embed = interaction.message.embeds[0]
            embed.add_field(name="결과", value="✅ 1팀 승리!", inline=False)
            embed.add_field(name="💸 배당 결과", value="배당 결과가 없습니다.", inline=False)
            await interaction.response.edit_message(embed=embed, view=self)

        @discord.ui.button(label="2팀 승리", style=discord.ButtonStyle.danger)
        async def team2_win(self, interaction: discord.Interaction, button: Button):
            if interaction.user.id != self.game.host_id and not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("개최자 또는 관리자만 결과를 기록할 수 있습니다.", ephemeral=True)
                return
            if self.game.finished:
                await interaction.response.send_message("이미 결과가 기록되었습니다.", ephemeral=True)
                return

            uids_team1 = list(set([self.game.team_captains[0]] + self.game.teams[1]))
            uids_team2 = list(set([self.game.team_captains[1]] + self.game.teams[2]))

            for uid in uids_team1:
                update_result_dual(str(uid), False)
            for uid in uids_team2:
                update_result_dual(str(uid), True)

            self.game.finished = True
            self.team1_win.disabled = True
            self.team2_win.disabled = True
            self.cancel_game.disabled = True
            self.add_item(MatchCog.PlayAgainButton(self.cog, self.game))
            self.add_item(MatchCog.RevengeButton(self.cog, self.game))
            self.add_item(MatchCog.EndGameButton(self.cog, self.game))

            embed = interaction.message.embeds[0]
            embed.add_field(name="결과", value="✅ 2팀 승리!", inline=False)
            embed.add_field(name="💸 배당 결과", value="배당 결과가 없습니다.", inline=False)
            await interaction.response.edit_message(embed=embed, view=self)

        @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
        async def cancel_game(self, interaction: discord.Interaction, button: Button):
            if interaction.user.id != self.game.host_id and not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("개최자 또는 관리자만 취소할 수 있습니다.", ephemeral=True)
                return
            if self.game.finished:
                await interaction.response.send_message("이미 결과가 기록되었습니다.", ephemeral=True)
                return

            self.game.finished = True
            self.team1_win.disabled = True
            self.team2_win.disabled = True
            self.cancel_game.disabled = True

            embed = interaction.message.embeds[0]
            embed.add_field(name="결과", value="❌ 게임이 취소되었습니다. 결과는 기록되지 않습니다.", inline=False)
            await interaction.response.edit_message(embed=embed, view=self)

    class PlayAgainButton(Button):
        def __init__(self, cog: "MatchCog", game: Game):
            super().__init__(label="팀다시뽑기!", style=discord.ButtonStyle.secondary)
            self.cog = cog
            self.game = game

        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != self.game.host_id and not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("개최자 또는 관리자만 한판 더 진행할 수 있습니다.", ephemeral=True)
                return

            old_game = self.game

            new_game_id = self.cog.game_counter
            self.cog.game_counter += 1

            new_game = Game(new_game_id, old_game.host_id, old_game.channel_id, old_game.max_players)
            new_game.participants = list(old_game.participants)
            self.cog.games[new_game_id] = new_game

            participants_list = ""
            for idx, user_id in enumerate(new_game.participants, 1):
                member = interaction.guild.get_member(user_id)
                if member:
                    participants_list += f"{idx}. {member.display_name}\n"

            embed = discord.Embed(
                title=f"내전 #{new_game_id} - {interaction.guild.get_member(new_game.host_id).display_name}",
                description=f"인원: {len(new_game.participants)}/{new_game.max_players}",
                color=0x2F3136
            )
            embed.add_field(name="참여자", value=participants_list or "아직 참여자가 없습니다.", inline=False)

            view = self.cog.LobbyView(self.cog, new_game)
            if new_game.is_full():
                view.clear_items()
                view = self.cog.StartEndView(self.cog, new_game)

            message = await interaction.channel.send(embed=embed, view=view)
            new_game.message = message

            end_embed = discord.Embed(title="내전 종료", description="✅ 새로운 내전이 생성되었습니다!", color=0x2F3136)
            await interaction.response.edit_message(embed=end_embed, view=None)

            for child in self.view.children:
                child.disabled = True

    class RevengeButton(Button):
        def __init__(self, cog: "MatchCog", game: Game):
            super().__init__(label="한판 더!", style=discord.ButtonStyle.success)
            self.cog = cog
            self.game = game

        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != self.game.host_id and not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("개최자 또는 관리자만 한판 더 진행할 수 있습니다.", ephemeral=True)
                return

            old_game = self.game

            new_game_id = self.cog.game_counter
            self.cog.game_counter += 1

            new_game = Game(new_game_id, old_game.host_id, old_game.channel_id, old_game.max_players)
            new_game.participants = list(old_game.participants)
            new_game.team_captains = list(old_game.team_captains)
            new_game.teams = {1: list(old_game.teams[1]), 2: list(old_game.teams[2])}
            new_game.started = True

            self.cog.games[new_game_id] = new_game

            guild = interaction.guild
            team1_members, team2_members = [], []

            for uid in new_game.teams[1]:
                member = guild.get_member(uid)
                name = member.display_name if member else "알 수 없음"
                if uid == new_game.team_captains[0]:
                    team1_members.insert(0, f"⭐ {name}")
                else:
                    team1_members.append(f"- {name}")

            for uid in new_game.teams[2]:
                member = guild.get_member(uid)
                name = member.display_name if member else "알 수 없음"
                if uid == new_game.team_captains[1]:
                    team2_members.insert(0, f"⭐ {name}")
                else:
                    team2_members.append(f"- {name}")

            t1 = "\n".join(team1_members)
            t2 = "\n".join(team2_members)

            embed = discord.Embed(title=f"내전 #{new_game_id} 한판 더 매치!", color=0x2F3136)
            embed.add_field(name="1팀", value=t1 or "- 없음", inline=True)
            embed.add_field(name="2팀", value=t2 or "- 없음", inline=True)

            view = self.cog.ResultView(self.cog, new_game)
            result_message = await interaction.channel.send(embed=embed, view=view)
            new_game.result_message = result_message

            asyncio.create_task(self.cog.disable_buttons_after_timeout(result_message, view, 10800))

            end_embed = discord.Embed(title="내전 종료", description="✅ 한판 더 매치가 생성되었습니다!", color=0x2F3136)
            await interaction.response.edit_message(embed=end_embed, view=None)
            await interaction.channel.send(view=self.cog.BettingView(new_game))

    class EndGameButton(Button):
        def __init__(self, cog: "MatchCog", game: Game):
            super().__init__(label="종료", style=discord.ButtonStyle.danger)
            self.cog = cog
            self.game = game

        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != self.game.host_id and not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("개최자 또는 관리자만 한판 더 진행할 수 있습니다.", ephemeral=True)
                return

            self.cog.active_hosts.discard(self.game.host_id)
            self.cog.games.pop(self.game.id, None)

            for child in self.view.children:
                child.disabled = True

            embed = interaction.message.embeds[0]
            embed.add_field(name="상태", value="🛑 게임이 종료되었습니다.", inline=False)
            await interaction.response.edit_message(embed=embed, view=self.view)
