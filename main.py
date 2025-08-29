import discord
from discord.ext import commands
from discord.ui import View, Button, Modal, TextInput, Select
import random
import json
import os
import asyncio
import re
import urllib.parse
from pathlib import Path
import json
from datetime import datetime, timedelta
import configparser

# from 포켓몬8 import subtract_points_from_user, save_user_data, get_user_points, place_bet, process_betting_result

config = configparser.ConfigParser()
config.read("config.ini", encoding="utf-8")

# 역할 ID (내전)
ROLE_IDS = {
    "사서": 1409174707307151418,
    "수석사서": 1409174707307151419,
    "큐레이터": 1409174707307151416,
    "관장": 1409174707315544064,
    "내전": 1409174707315544065,
}

# 섹션 자동 탐지: [discord] 있으면 그걸, 아니면 [Settings] 사용
section = "Settings"

TOKEN = (
    os.getenv("DISCORD_TOKEN")
    or (config.get(section, "token", fallback="").strip())
)
 
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

game_counter = 1
games = {}  
active_hosts = set()  

STATS_FILE = "user_stats.json"
BASE_DIR = Path(__file__).resolve().parent
STATS_PATH = BASE_DIR / STATS_FILE

# 사용자 라이엇 아이디, 첫 '/' 전까지 캡쳐
RIOT_ID_RE = re.compile(r'^\s*(?P<riot>[^/\n]+?)(?:/|$)')

def extract_riot_id(display_name: str) -> str | None:
    """디스플레이 네임에서 '소환사명#태그'만 추출하고 태그 오탈자 보정."""
    m = RIOT_ID_RE.search(display_name or "")
    if not m:
        return None
    riot = m.group("riot").strip()

    if "#" not in riot:   # 태그가 없으면 형식 오류
        return None

    name, tag = riot.split("#", 1)
    tag = tag.strip().upper()

    # 흔한 오타 보정 (예: K1R -> KR1, KRl(엘) -> KR1)
    if tag in {"K1R", "KRI", "KRL", "KRl"}:
        tag = "KR1"

    return f"{name.strip()}#{tag}"

def load_stats():
    if not STATS_PATH.exists():
        return {}
    try:
        with open(STATS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except UnicodeDecodeError:
        with open(STATS_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}

def save_stats(data):
    with open(STATS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def update_result_dual(user_id, won):
    def load_json(path):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def save_json(path, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    for file in ["user_stats.json", "mang.json"]:
        stats = load_json(file)
        if user_id not in stats:
            stats[user_id] = {"참여": 0, "승리": 0, "패배": 0}
        stats[user_id]["참여"] += 1
        if won:
            stats[user_id]["승리"] += 1
        else:
            stats[user_id]["패배"] += 1
        save_json(file, stats)


class Game:
    def __init__(self, game_id, host_id, channel_id, max_players=10):
        self.id = game_id
        self.host_id = host_id
        self.channel_id = channel_id
        self.max_players = max_players
        self.participants = [host_id]
        self.started = False
        self.message = None
        self.team_captains = []
        self.teams = {1: [], 2: []}
        self.pick_order = []
        self.draft_turn = 0
        self.finished = False
        self.result_message = None
        self.team_status_message = None
        self.bets = {}  


    def is_full(self):
        return len(self.participants) >= self.max_players

    def add_participant(self, user_id):
        if user_id not in self.participants and not self.is_full():
            self.participants.append(user_id)
            return True
        return False

    def remove_participant(self, user_id):
        if user_id in self.participants and user_id != self.host_id:
            self.participants.remove(user_id)
            return True
        return False


class LobbyView(View):
    def __init__(self, game):
        super().__init__(timeout=None)
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
                sorted_list = await get_sorted_participants_by_tier(interaction.guild, self.game.participants)
                embed = discord.Embed(title="📋 티어 기준 정렬된 참여자", color=0x2F3136)
                embed.description = "\n".join([f"{i+1}. {entry}" for i, entry in enumerate(sorted_list)])
                await interaction.channel.send(embed=embed)

                self.clear_items()
                await self.game.message.edit(view=StartEndView(self.game))

                for user_id in self.game.participants:
                    member = interaction.guild.get_member(user_id)
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

            # 로그 채널 ID는 필요에 따라 수정하세요
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
        games.pop(self.game.id, None)
        active_hosts.remove(self.game.host_id)

class StartEndView(View):
    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game
        self.add_item(Button(label="시작", style=discord.ButtonStyle.primary, custom_id="start"))
        self.add_item(Button(label="종료", style=discord.ButtonStyle.danger, custom_id="end"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.data["custom_id"] == "start":
            if interaction.user.id != self.game.host_id:
                await interaction.response.send_message("게임 시작은 개최자만 가능합니다.", ephemeral=True)
                return False
            self.game.started = True

            embed = discord.Embed(
                title="팀장 선택",
                description="팀장 선택을 시작합니다!",
                color=0x2F3136
            )
            await interaction.response.edit_message(embed=embed, view=None)
            await start_team_leader_selection(interaction, self.game)
            return True

        elif interaction.data["custom_id"] == "end":
            if interaction.user.id != self.game.host_id:
                await interaction.response.send_message("이 명령은 개최자만 실행할 수 있습니다.", ephemeral=True)
                return False

            embed = discord.Embed(
                title="내전 모집 취소",
                description="내전 모집이 취소되었습니다.",
                color=0x2F3136
            )
            await interaction.response.edit_message(embed=embed, view=None)
            games.pop(self.game.id, None)
            active_hosts.remove(self.game.host_id)
            return True

        return True
    
# 티어순 정렬
async def get_sorted_participants_by_tier(guild, user_ids):
    tier_order = {
        "C": 0, "GM": 1, "M": 2, "D": 3, "E": 4, "P": 5,
        "G": 6, "S": 7, "B": 8, "I": 9
    }

    def parse_tier(text):
        import re
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



async def start_team_leader_selection(interaction, game):
    guild = interaction.guild

    sorted_names = await get_sorted_participants_by_tier(guild, game.participants)

    name_to_user = {guild.get_member(uid).display_name: uid for uid in game.participants if guild.get_member(uid)}

    options = []
    for name in sorted_names:
        uid = name_to_user.get(name)
        if uid:
            options.append(discord.SelectOption(label=name, value=str(uid)))

    class CaptainSelectView(View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.select(placeholder="팀장을 선택하세요 (두 명)", min_values=2, max_values=2, options=options)
        async def select_callback(self, interaction: discord.Interaction, select: Select):
            if interaction.user.id != game.host_id:
                await interaction.response.send_message("팀장 선택은 개최자만 가능합니다.", ephemeral=True)
                return

            game.team_captains = [int(uid) for uid in select.values]

            embed = discord.Embed(
                title="팀장 선택 완료",
                description="팀장이 선택되었습니다! 팀 구성을 시작합니다.",
                color=0x2F3136
            )
            await interaction.response.edit_message(embed=embed, view=None)
            await start_draft(interaction, game)

    embed = discord.Embed(
        title="팀장 선택",
        description="티어 순으로 정렬된 명단에서 팀장을 선택해주세요:",
        color=0x2F3136
    )
    await interaction.channel.send(embed=embed, view=CaptainSelectView())


async def start_draft(interaction, game):
    players = [uid for uid in game.participants if uid not in game.team_captains]
    random.shuffle(players)
    first = random.choice([1, 2])

    random.shuffle(game.team_captains)
    game.teams[1].append(game.team_captains[0])
    game.teams[2].append(game.team_captains[1])

    if first == 1:
        game.pick_order = [1, 2, 2, 1, 1, 2, 2, 1]
    else:
        game.pick_order = [2, 1, 1, 2, 2, 1, 1, 2]

    guild = interaction.guild
    c1 = guild.get_member(game.team_captains[0]).display_name
    c2 = guild.get_member(game.team_captains[1]).display_name
    embed = discord.Embed(title=f"내전 #{game.id} 팀 구성 현황", color=0x2F3136)
    embed.add_field(name="1팀", value=f"- {c1}", inline=True)
    embed.add_field(name="2팀", value=f"- {c2}", inline=True)

    game.team_status_message = await interaction.channel.send(embed=embed)

    await send_draft_ui(interaction.channel, game, players)

async def send_draft_ui(channel, game, available):
    if not available or game.draft_turn >= len(game.pick_order):
        await finish_teams(channel, game)
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

            game.teams[team_num].append(uid)
            available.remove(uid)
            game.draft_turn += 1

            await game.team_status_message.edit(embed=create_team_embed())

            await interaction.message.delete()

            await send_draft_ui(channel, game, available)

    embed = discord.Embed(
        title=f"{team_num}팀 팀원 선택",
        description=f"{guild.get_member(captain_id).display_name}님, 팀원을 선택하세요:",
        color=0x2F3136
    )
    await channel.send(embed=embed, view=DraftView())


def create_opgg_multisearch_url(summoner_list):
    base_url = "https://op.gg/ko/lol/multisearch/kr?summoners="
    encoded = [urllib.parse.quote(s) for s in summoner_list]
    return base_url + ",".join(encoded)

class OpggButtonView(discord.ui.View):
    def __init__(self, url1, url2, timeout=10800):
        super().__init__(timeout=timeout)
        self.add_item(discord.ui.Button(label="🔎 1팀 전적 보기", url=url1, style=discord.ButtonStyle.link))
        self.add_item(discord.ui.Button(label="🔎 2팀 전적 보기", url=url2, style=discord.ButtonStyle.link))


def clean_opgg_name(name):
    return re.sub(r"[^\w\s가-힣/#]", "", name).split('/')[0].strip()

async def finish_teams(channel, game):
    guild = channel.guild

    team1_members = []
    team2_members = []
    team1_opgg_names = []
    team2_opgg_names = []

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

    result_view = ResultView(game)
    result_message = await channel.send(embed=embed, view=result_view)
    game.result_message = result_message

    opgg_view = OpggButtonView(opgg1, opgg2)
    await channel.send(view=opgg_view)

    asyncio.create_task(disable_buttons_after_timeout(result_message, result_view, 10800))

    await channel.send(view=BettingView(game))


async def disable_buttons_after_timeout(message, view, seconds):
    await asyncio.sleep(seconds)

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


class ResultView(View):
    def __init__(self, game):
        super().__init__(timeout=None)
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
        self.add_item(PlayAgainButton(self.game))
        self.add_item(RevengeButton(self.game))
        self.add_item(EndGameButton(self.game))

        embed = interaction.message.embeds[0]
        embed.add_field(name="결과", value="✅ 1팀 승리!", inline=False)

        # 배팅 시스템이 구현되지 않았으므로 주석 처리
        # 결과들 = process_betting_result(1, self.game)  
        # if 결과들:
        #     embed.add_field(name="💸 배당 결과", value="\n".join(결과들), inline=False)
        # else:
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
        self.add_item(PlayAgainButton(self.game))
        self.add_item(RevengeButton(self.game))
        self.add_item(EndGameButton(self.game))

        embed = interaction.message.embeds[0]
        embed.add_field(name="결과", value="✅ 2팀 승리!", inline=False)

        # 배팅 시스템이 구현되지 않았으므로 주석 처리
        # 결과들 = process_betting_result(2, self.game)
        # if 결과들:
        #     embed.add_field(name="💸 배당 결과", value="\n".join(결과들), inline=False)
        # else:
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
    def __init__(self, game):
        super().__init__(label="팀다시뽑기!", style=discord.ButtonStyle.secondary)
        self.game = game

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.game.host_id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("개최자 또는 관리자만 한판 더 진행할 수 있습니다.", ephemeral=True)
            return


        global game_counter
        old_game = self.game

        new_game_id = game_counter
        game_counter += 1

        new_game = Game(new_game_id, old_game.host_id, old_game.channel_id, old_game.max_players)

        new_game.participants = list(old_game.participants)
        games[new_game_id] = new_game

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

        view = LobbyView(new_game)
        if new_game.is_full():
            view.clear_items()
            view = StartEndView(new_game)

        message = await interaction.channel.send(embed=embed, view=view)
        new_game.message = message

        end_embed = discord.Embed(
            title="내전 종료",
            description="✅ 새로운 내전이 생성되었습니다!",
            color=0x2F3136
        )
        await interaction.response.edit_message(embed=end_embed, view=None)

        for child in self.view.children:
            child.disabled = True


class RevengeButton(Button):
    def __init__(self, game):
        super().__init__(label="한판 더!", style=discord.ButtonStyle.success)
        self.game = game

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.game.host_id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("개최자 또는 관리자만 한판 더 진행할 수 있습니다.", ephemeral=True)
            return


        global game_counter
        old_game = self.game

        new_game_id = game_counter
        game_counter += 1

        new_game = Game(new_game_id, old_game.host_id, old_game.channel_id, old_game.max_players)

        new_game.participants = list(old_game.participants)

        new_game.team_captains = list(old_game.team_captains)
        new_game.teams = {1: list(old_game.teams[1]), 2: list(old_game.teams[2])}
        new_game.started = True

        games[new_game_id] = new_game

        guild = interaction.guild
        c1 = guild.get_member(new_game.team_captains[0])
        c2 = guild.get_member(new_game.team_captains[1])

        team1_members = []
        team2_members = []

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

        view = ResultView(new_game)
        result_message = await interaction.channel.send(embed=embed, view=view)
        new_game.result_message = result_message

        asyncio.create_task(disable_buttons_after_timeout(result_message, view, 10800))

        end_embed = discord.Embed(
            title="내전 종료",
            description="✅ 한판 더 매치가 생성되었습니다!",
            color=0x2F3136
        )
        await interaction.response.edit_message(embed=end_embed, view=None)
        await interaction.channel.send(view=BettingView(new_game))

class EndGameButton(Button):
    def __init__(self, game):
        super().__init__(label="종료", style=discord.ButtonStyle.danger)
        self.game = game

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.game.host_id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("개최자 또는 관리자만 한판 더 진행할 수 있습니다.", ephemeral=True)
            return

        global active_hosts

        if self.game.host_id in active_hosts:
            active_hosts.remove(self.game.host_id)

        games.pop(self.game.id, None)

        for child in self.view.children:
            child.disabled = True

        embed = interaction.message.embeds[0]
        embed.add_field(name="상태", value="🛑 게임이 종료되었습니다.", inline=False)

        await interaction.response.edit_message(embed=embed, view=self.view)

@bot.command()
async def 내전(ctx):
    global game_counter
    game_id = game_counter
    game_counter += 1

    game = Game(game_id, ctx.author.id, ctx.channel.id)
    games[game_id] = game
    active_hosts.add(ctx.author.id)

    participants_list = f"1. {ctx.author.display_name}\n"

    embed = discord.Embed(
        title=f"내전 #{game_id} - {ctx.author.display_name}",
        description=f"인원: 1/{game.max_players}",
        color=0x2F3136
    )
    embed.add_field(name="참여자", value=participants_list, inline=False)

    view = LobbyView(game)

    role_id = ROLE_IDS.get("내전")
    role = ctx.guild.get_role(role_id)

    if role is None:
        role = discord.utils.get(ctx.guild.roles, name="내전")

    allowed = discord.AllowedMentions(roles=[role])  # 이 역할만 멘션 허용
    content = role.mention  # '<@&역할ID>' 형태

    message = await ctx.send(
        content=content,
        embed=embed,
        view=view,
        allowed_mentions=allowed
    )
    game.message = message

@bot.command()
async def 전적(ctx, member: discord.Member = None):
    stats = load_stats()
    if member is None:
        member = ctx.author
    uid = str(member.id)
    s = stats.get(uid, {"참여": 0, "승리": 0, "패배": 0})
    total = s["참여"]
    win = s["승리"]
    lose = s["패배"]
    rate = round(win / total * 100, 2) if total else 0

    embed = discord.Embed(
        title=f"{member.display_name} 전적",
        color=0x2F3136
    )
    embed.add_field(name="참여", value=f"{total}회", inline=True)
    embed.add_field(name="승", value=f"{win}", inline=True)
    embed.add_field(name="패", value=f"{lose}", inline=True)
    embed.add_field(name="승률", value=f"{rate}%", inline=True)

    await ctx.send(embed=embed)


@bot.command(name="내전랭킹")
async def 내전랭킹(ctx):
    stats = load_stats()
    members = [(int(uid), data) for uid, data in stats.items() if data["참여"] >= 20]
    if not members:
        embed = discord.Embed(
            title="내전랭킹",
            description="참여 5회 이상 유저가 없습니다.",
            color=0x2F3136
        )
        await ctx.send(embed=embed)
        return

    sorted_list = sorted(members, key=lambda x: (x[1]["승리"] / x[1]["참여"] if x[1]["참여"] else 0), reverse=True)
    top10 = sorted_list[:20]

    embed = discord.Embed(
        title="승률 TOP 10 (참여 20회 이상)",
        color=0x2F3136
    )

    for idx, (uid, data) in enumerate(top10, 1):
        member = ctx.guild.get_member(uid)
        if member:
            winrate = round(data["승리"] / data["참여"] * 100, 2)
            embed.add_field(
                name=f"{idx}. {member.display_name}",
                value=f"승률: {winrate}%\n참여: {data['참여']}, 승: {data['승리']}, 패: {data['패배']}",
                inline=False
            )

    await ctx.send(embed=embed)

def generate_내전랭킹_embed(guild):
    stats = load_stats()

    members = [(int(uid), data) for uid, data in stats.items() if data.get("참여", 0) >= 15]

    if not members:
        embed = discord.Embed(
            title="⚔️ 내전 랭킹",
            description="참여 15회 이상 유저가 없습니다.",
            color=0x2F3136
        )
        return embed

    sorted_list = sorted(
        members,
        key=lambda x: (x[1]["승리"] / x[1]["참여"]) if x[1]["참여"] else 0,
        reverse=True
    )

    top20 = sorted_list[:20]  

    embed = discord.Embed(title="⚔️ 내전 랭킹 (Top 20)", color=discord.Color.blurple())

    for i, (uid, data) in enumerate(top20, 1):
        member = guild.get_member(uid)
        name = member.display_name if member else f"탈퇴자({uid})"
        win_rate = data["승리"] / data["참여"] * 100
        embed.add_field(
            name=f"{i}. {name}",
            value=f"승률: {win_rate:.1f}%\n{data['승리']}승 / {data['참여']}전",
            inline=False
        )

    return embed


def generate_내전판수_embed(guild):
    stats = load_stats()

    members = [(int(uid), data) for uid, data in stats.items() if data.get("참여", 0) > 0]

    if not members:
        embed = discord.Embed(
            title="📊 내전 판수 랭킹",
            description="참여한 유저가 없습니다.",
            color=0x2F3136
        )
        return embed

    sorted_members = sorted(members, key=lambda x: x[1]["참여"], reverse=True)
    top20 = sorted_members[:20]  

    embed = discord.Embed(title="📊 내전 판수 랭킹 (Top 20)", color=discord.Color.red()) 

    for i, (uid, data) in enumerate(top20, 1): 
        member = guild.get_member(uid)
        name = member.display_name if member else f"탈퇴자({uid})"
        wins = data.get("승리", 0)
        losses = data.get("패배", 0)
        total = data.get("참여", 0)

        embed.add_field(
            name=f"{i}. {name}",
            value=f"{total}전 ({wins}승 / {losses}패)",
            inline=False
        )

    return embed


@bot.event
async def on_ready():
    print(f"봇 로그인됨: {bot.user}")


def load_bad_words():
    try:
        with open("bad_words.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("bad_words", [])
    except (FileNotFoundError, json.JSONDecodeError):
        with open("bad_words.json", "w", encoding="utf-8") as f:
            json.dump({"bad_words": []}, f, ensure_ascii=False, indent=4)
        return []


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    role_titles = {
        "지우": "지우군",
        "빛나": "빛나양"
    }

    title = message.author.display_name
    for role in message.author.roles:
        if role.name in role_titles:
            title = role_titles[role.name]
            break

    bad_words = load_bad_words()
    message_words = message.content.lower().split()

    if any(bad_word.lower().strip() in message_words for bad_word in bad_words):
        await message.channel.send(f"{message.author.mention} \n{title} 말 좀 예뿌게 하세요~ <:57:1357677118028517488>")


@bot.command(name="스팸추가")
@commands.has_permissions(administrator=True)
async def add_bad_word(ctx, *, word):
    try:
        bad_words = load_bad_words()
        word = word.strip().lower()

        if word in [w.strip().lower() for w in bad_words]:
            await ctx.send("이미 등록된 단어입니다.")
            return

        bad_words.append(word)
        with open("bad_words.json", "w", encoding="utf-8") as f:
            json.dump({"bad_words": bad_words}, f, ensure_ascii=False, indent=4)
        await ctx.send(f"`{word}` 추가 완료")
    except Exception as e:
        await ctx.send(f"오류가 발생했습니다: {str(e)}")


@bot.command(name="스팸삭제")
@commands.has_permissions(administrator=True)
async def remove_bad_word(ctx, *, word):
    try:
        bad_words = load_bad_words()
        word = word.strip().lower()

        bad_words_lower = [w.strip().lower() for w in bad_words]

        if word not in bad_words_lower:
            await ctx.send("등록되지 않은 단어입니다.")
            return

        index = bad_words_lower.index(word)
        removed_word = bad_words[index]
        bad_words.pop(index)

        with open("bad_words.json", "w", encoding="utf-8") as f:
            json.dump({"bad_words": bad_words}, f, ensure_ascii=False, indent=4)
        await ctx.send(f"`{removed_word}` 삭제 완료")
    except Exception as e:
        await ctx.send(f"오류가 발생했습니다: {str(e)}")

돌_대답 = [
    "구르는 돌은 방향을 잊어요. 그래서 언덕을 오를 수 없어요.",
    "깊게 가라앉은 돌은, 얕은 물을 싫어했어요.",
    "모든 돌은 언젠가 모래가 되지만, 모래는 돌을 그리워하지 않아요.",
    "크기가 중요한 게 아니에요. 결국 다 눕잖아요.",
    "세상 모든 돌은 둥글어지길 꿈꿨지만, 몇 도가 필요한지는 몰랐어요.",
    "강한 돌도 가끔은 뒤집히고 싶어 해요. 배는 없지만요.",
    "부드러운 돌이 되고 싶으면, 그냥 세게 굴려보세요. 어떻게든 돼요.",
    "가장 무거운 돌도 결국 누워서 지구를 바라봅니다.",
    "깨진 돌은 아파하지 않아요. 대신 더 깨질 준비를 하죠.",
    "바람을 거슬러 서 있는 돌은 없습니다. 그냥 무거운 거예요.",
    "산 꼭대기의 돌도, 사실은 그냥 거기 굴러간 거예요.",
    "작은 돌도 언젠가는 큰 그림자를 가질 수 있어요. 해만 진다면요.",
    "돌은 흘러가는 물을 이해하지 못해요. 그래서 가만히 있어요.",
    "길가에 버려진 돌은 불행하지 않아요. 걍 관심이 없어요.",
    "돌은 왜 사는지 묻지 않아요. 그냥 계속 있어요.",
    "뛰어내리는 돌은 없습니다. 뛰지 못하니까요.",
    "어떤 돌은 바람을 타려고 해요. 실패합니다.",
    "모든 돌은 떨어질 수 있어요. 문제는 높이가 아니라 타이밍이에요.",
    "돌이 생각하는 걸 본 사람은 없어요. 왜냐하면 진짜 생각 안 하거든요.",
    "끊임없이 가만히 있는 것도 일종의 노력입니다. 아님 말고요.",
    "돌이 웃으면 기분 나쁘겠죠? 다행히 웃지 않아요.",
    "비바람을 견디는 돌이 대단해 보이나요? 사실 신경도 안 써요.",
    "정체된 돌도 언젠가는 깨져요. 그러니까 너무 걱정 마요.",
    "인생이 굴러가면 좋은 거예요. 돌처럼 구르면요.",
    "세상에 가장 빠른 돌은 없어요. 돌은 빠를 수가 없어요.",
    "돌끼리 대화하지 않는 이유는, 서로 할 말이 없기 때문이에요.",
    "너무 깊이 생각하지 마세요. 돌도 안 하잖아요.",
    "변하는 돌도 있어요. 그걸 사람들은 자갈이라고 부르죠.",
    "실수한 돌은 없습니다. 애초에 아무것도 안 하니까요.",
    "바람을 두려워하는 돌은 없어요. 느끼지 못하거든요.",
    "구르는 돌은 이끼가 끼지 않지만, 이끼는 신경 안 써요.",
    "깨져본 돌만이 아픔을 모른다는 걸 알아요. 왜냐면 돌이라서요.",
    "돌 위에 핀 꽃은 돌을 고맙게 생각하지 않아요.",
    "가장 오래 살아남은 돌도 사실 아무것도 안 했어요.",
    "물결은 바위에 부딪히고, 바위는 그냥 있습니다.",
    "돌의 목표는 없습니다. 목표가 뭔지도 모릅니다.",
    "힘들면 그냥 누워요. 돌은 항상 누워있어요.",
    "세상 모든 문제는 돌에게 없습니다. 이해를 못 하니까요.",
    "돌은 걷지 않아요. 대신 기다리지도 않아요.",
    "어차피 흘러가는 세상, 돌은 그냥 눕습니다.",
    "무너지지 않는 돌은 없어요. 단지 시간이 걸릴 뿐이에요.",
    "바람에 흔들리는 돌은 없어요. 흔들릴 수가 없거든요.",
    "길모퉁이에 있는 돌은 방향을 고민하지 않아요. 그냥 있습니다.",
    "누가 발로 차도 돌은 화내지 않아요. 대신 굴러갑니다.",
    "산 아래 있는 돌은 꼭대기 돌을 부러워하지 않아요.",
    "젖은 돌도, 마른 돌도 결국 그냥 돌이에요.",
    "모래가 되는 걸 두려워하지 않는 돌이 진짜 돌이에요.",
    "돌은 과거를 잊지 않아요. 애초에 기억하지 않으니까요.",
    "돌은 앞날을 걱정하지 않아요. 그냥 계속 존재해요.",
    "돌은 비교하지 않아요. 크든 작든 그냥 존재할 뿐이에요.",
    "아무것도 바라지 않는 돌이 가장 강합니다.",
    "움직이지 않아도 세상은 돌을 지나쳐요.",
    "어떤 돌은 하늘을 꿈꾸지만, 결국 땅에 눕습니다.",
    "바닥에 붙어 있는 돌이 높은 돌을 부러워할까요? 관심 없습니다.",
    "바다에 빠진 돌은 헤엄치려 하지 않아요.",
    "언젠가 깨어질 것을 알면서도 돌은 그냥 있습니다.",
    "구르는 돌도 언젠가는 멈춰요. 그게 인생이에요.",
    "돌의 침묵은 무거운 게 아니라, 그냥 돌이라서 그런 거예요.",
    "어디에 있든 돌은 돌이에요. 장소는 중요하지 않아요.",
    "돌은 기다리지 않지만, 시간이 돌을 기다립니다.",
    "세상의 모든 돌은 결국 같은 흙으로 돌아갑니다.",
    "깨어진 돌은 슬퍼하지 않아요. 새로운 모습일 뿐이에요.",
    "움직이지 않는 돌도 세상을 바꿀 수 있어요. 사람 발목을 잡아서요.",
    "돌은 빛나지 않아도 존재합니다. 그걸로 충분해요.",
    "하늘을 올려다보는 돌은 없습니다. 고개를 들 수 없으니까요.",
    "바다 속 돌은 별을 본 적 없어요. 근데 별로 궁금하지도 않아요."
]

from collections import deque

최근_대답 = deque(maxlen=5)

@bot.command()
async def 고민(ctx, *, 내용):
    후보 = [대답 for 대답 in 돌_대답 if 대답 not in 최근_대답]

    if not 후보:
        후보 = 돌_대답

    대답 = random.choice(후보)
    최근_대답.append(대답)

    await ctx.send(f"🪨 대답하는 돌멩이:\n {대답}")


class BettingView(View):
    def __init__(self, game):
        super().__init__(timeout=210)
        self.game = game

    @discord.ui.button(label="1팀에 배팅", style=discord.ButtonStyle.success)
    async def bet_team1(self, interaction: discord.Interaction, button: Button):
        await self.handle_bet(interaction, team=1)

    @discord.ui.button(label="2팀에 배팅", style=discord.ButtonStyle.success)
    async def bet_team2(self, interaction: discord.Interaction, button: Button):
        await self.handle_bet(interaction, team=2)

    async def handle_bet(self, interaction, team):
        
        class BetModal(Modal, title="배팅 금액 입력"):
            amount = TextInput(label="배팅할 금액", placeholder="숫자만 입력 (최소 1000₽)", required=True)

            def __init__(self, game, team):
                super().__init__()
                self.game = game
                self.team = team

            async def on_submit(self, modal_interaction):
                user_id = modal_interaction.user.id

                try:
                    amount_int = int(self.amount.value)
                    if amount_int < 1000:
                        await modal_interaction.response.send_message(
                            "❌ 최소 배팅 금액은 1000₽입니다.", ephemeral=True)
                        return
                except:
                    await modal_interaction.response.send_message(
                        "❌ 숫자만 입력해 주세요.", ephemeral=True)
                    return

                if user_id in self.game.bets:
                    await modal_interaction.response.send_message(
                        "❌ 이미 배팅하셨습니다.", ephemeral=True)
                    return

                # 포인트 시스템이 구현되지 않았으므로 주석 처리
                #if not subtract_points_from_user(user_id, amount_int):
                #    await modal_interaction.response.send_message(
                #        "❌ 포인트가 부족합니다.", ephemeral=True)
                #    return

                self.game.bets[user_id] = {
                    "amount": amount_int,
                    "team": self.team
                }

                await modal_interaction.response.send_message(
                    f"✅ {modal_interaction.user.mention}님이 {self.team}팀에 {amount_int}₽ 배팅했습니다.",
                    ephemeral=False
                )

        await interaction.response.send_modal(BetModal(self.game, team))


@bot.command(name="스크림")
async def 스크림(ctx):
    import os
    import json

    def load_mang():
        if os.path.exists("mang.json"):
            with open("mang.json", "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    stats = load_mang()
    user_id = str(ctx.author.id)

    if user_id not in stats:
        await ctx.send("❌ 스크림에 참여한 기록이 없습니다.")
        return

    data = stats[user_id]
    total = data.get("참여", 0)
    wins = data.get("승리", 0)
    losses = total - wins

    if total == 0:
        await ctx.send("❌ 스크림에 참여한 기록이 없습니다.")
        return

    winrate = round(wins / total * 100, 2)

    embed = discord.Embed(
        title=f"🎮 {ctx.author.display_name}님의 스크림 전적",
        color=discord.Color.dark_red()
    )
    embed.add_field(name="참여", value=f"{total}전", inline=True)
    embed.add_field(name="승리", value=f"{wins}승", inline=True)
    embed.add_field(name="패배", value=f"{losses}패", inline=True)
    embed.add_field(name="승률", value=f"{winrate}%", inline=False)

    await ctx.send(embed=embed)

# 주사위
@bot.command(name="주사위")
async def 주사위(ctx: commands.Context):
    outcomes = ["1", "2", "3", "4", "5", "6", "꽝", "999"]
    weights  = [16, 16, 16, 16, 16, 16,   2,   2]

    result = random.choices(outcomes, weights=weights, k=1)[0]

    dice = ":game_die:"

    if result in {"1", "2", "3", "4", "5", "6"}:
        await ctx.send(f"{dice} 주사위 {result}!")
    elif result == "꽝":
        await ctx.send(f"{dice}꽝~ 모솔 맘사위 당첨!")
    else:  # result == "999"
        await ctx.send(f"{dice}999!! 무적 밀사위 당첨~")

def _has_cleanup_power(member: discord.Member) -> bool:
    role_ids = {r.id for r in member.roles}
    allowed = set(ROLE_IDS.values())
    return bool(role_ids & allowed) or member.guild_permissions.administrator

class ConfirmCleanView(discord.ui.View):
    def __init__(self, ctx: commands.Context, amount: int, *, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.amount = amount

    async def _deny_others(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("이 명령은 작성자만 실행할 수 있습니다.", ephemeral=True)
            return True
        return False

    @discord.ui.button(label="예", style=discord.ButtonStyle.danger, emoji="🧹")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._deny_others(interaction):
            return

        # 봇 권한 확인 (메시지 관리)
        perms = self.ctx.channel.permissions_for(self.ctx.me)
        if not perms.manage_messages:
            await interaction.response.send_message("❌ 봇에 **메시지 관리** 권한이 없습니다.", ephemeral=True)
            return

        # 먼저 비공개로 승인 알림
        await interaction.response.send_message("삭제를 시작합니다…", ephemeral=True)

        # 확인 메시지(버튼)와 명령 메시지 먼저 정리
        try:
            await interaction.message.delete()      # 확인창 삭제
        except discord.HTTPException:
            pass
        try:
            await self.ctx.message.delete()         # !청소 명령 메시지 삭제
        except discord.HTTPException:
            pass

        # 실제 메시지 삭제 (요청한 개수만)
        try:
            deleted = await self.ctx.channel.purge(limit=self.amount)
            count = len(deleted)
            await interaction.followup.send(f"✅ {count}개의 메시지를 삭제했습니다.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ 삭제 중 권한 오류가 발생했습니다.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ 삭제 중 오류: {e}", ephemeral=True)

        # 뷰 종료
        self.stop()

    @discord.ui.button(label="아니오", style=discord.ButtonStyle.secondary, emoji="✋")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._deny_others(interaction):
            return
        await interaction.response.send_message("작업을 취소했습니다.", ephemeral=True)
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass
        self.stop()

# 청소 명령어
@bot.command(name="청소")
async def 청소(ctx: commands.Context, amount: int):
    # 권한 체크
    if not _has_cleanup_power(ctx.author):
        try:
            await ctx.author.send("이 명령어를 사용할 권한이 없습니다.")
        except discord.Forbidden:
            await ctx.reply("이 명령어를 사용할 권한이 없습니다.", delete_after=4)
        return

    # 입력값 체크
    if not (1 <= amount <= 500):
        try:
            await ctx.author.send("1 ~ 500 사이의 숫자를 입력해주세요.")
        except discord.Forbidden:
            await ctx.reply("1 ~ 500 사이의 숫자를 입력해주세요.", delete_after=4)
        return

    # 확인 UI 띄우기 (공개로 띄우되, 버튼 클릭은 작성자만/응답은 비공개)
    embed = discord.Embed(
        title="정말로 지우시겠습니까?",
        description=f"이 채널에서 최근 **{amount}개**의 메시지가 삭제됩니다.",
        color=discord.Color.red()
    )
    view = ConfirmCleanView(ctx, amount)
    prompt = await ctx.send(embed=embed, view=view)

    # 30초(뷰 타임아웃) 지나면 확인창 자동 삭제
    async def _cleanup_when_timeout():
        await view.wait()
        # 뷰가 아직 살아있고 메시지가 남아있으면 정리
        if prompt and prompt.channel and any(i for i in view.children):
            try:
                await prompt.delete()
            except discord.HTTPException:
                pass
    bot.loop.create_task(_cleanup_when_timeout())

# 인수 누락 등 에러를 작성자에게만 안내
@청소.error
async def 청소_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingRequiredArgument):
        try:
            await ctx.author.send("사용법: `!청소 <1~500>`")
        except discord.Forbidden:
            await ctx.reply("사용법: `!청소 <1~500>`", delete_after=4)

# 내정보 명령어
@bot.command(name="내정보")
async def 내정보(ctx, member: discord.Member | None = None):
    target = member or ctx.author
    riot_id = extract_riot_id(target.display_name)

    if not riot_id:
        await ctx.send(
            "❌ 닉네임 형식이 올바르지 않습니다.\n"
            "예시: `소환사명#KR1/티어/라인`  (예: `김밀레#KR1/M575/TOP, JG`)"
        )
        return

    # FOW 링크 생성 (공백, 한글 포함 안전 인코딩)
    encoded = urllib.parse.quote(riot_id, safe="")
    url = f"https://fow.lol/find/{encoded}"

    embed = discord.Embed(
        title=f"{riot_id} 전적",
        description="아래 버튼을 눌러 **FOW.LOL**에서 자세한 전적을 확인하세요.",
        color=0x2F3136
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    view = View()
    view.add_item(Button(label="FOW.LOL에서 전적 확인하기", url=url, emoji="🖱️"))

    await ctx.send(embed=embed, view=view)

# 다른 사용자 정보 명령어
@bot.command(name="정보")
async def 정보(ctx, member: discord.Member):
    """멘션한 사용자의 닉네임(#태그) → FOW 링크로 안내"""
    riot_id = extract_riot_id(member.display_name)
    if not riot_id:
        await ctx.send(
            "❌ 닉네임 형식이 올바르지 않습니다.\n"
            "예시: `소환사명#KR1/티어/라인`  (예: `김밀레#KR1/M575/TOP, JG`)"
        )
        return

    encoded = urllib.parse.quote(riot_id, safe="")
    url = f"https://fow.lol/find/{encoded}"

    embed = discord.Embed(
        title=f"{riot_id} 전적",
        description="아래 버튼을 눌러 **FOW.LOL**에서 자세한 전적을 확인하세요.",
        color=0x2F3136
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    view = View()
    view.add_item(Button(label="FOW.LOL에서 전적 확인하기", url=url, emoji="🖱️"))

    await ctx.send(embed=embed, view=view)

# 사용법을 안내하는 에러 핸들러(멘션 안 줬을 때)
@정보.error
async def 정보_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("사용법: `!정보 @사용자`")

if __name__ == "__main__":
    bot.run(TOKEN)