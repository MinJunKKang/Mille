# cogs/stats_view.py
import discord
import re
from discord.ext import commands
from typing import Optional
import urllib.parse

from utils.stats import load_stats, ensure_user


RIOT_ID_RE = re.compile(r'^\s*(?P<riot>[^/\n]+?)(?:/|$)')

def extract_riot_id(display_name: str) -> Optional[str]:
    """디스플레이 네임에서 '소환사명#태그'만 추출하고 태그 오탈자 보정."""
    m = RIOT_ID_RE.search(display_name or "")
    if not m:
        return None
    riot = m.group("riot").strip()
    if "#" not in riot:
        return None

    name, tag = riot.split("#", 1)
    tag = tag.strip().upper()
    if tag in {"K1R", "KRI", "KRL", "KRl"}:
        tag = "KR1"
    return f"{name.strip()}#{tag}"

class StatsCog(commands.Cog):
    """유저 전적 / 내전 랭킹"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="전적", aliases=["정보"])
    async def stats_command(self, ctx: commands.Context, member: discord.Member | None = None):
        target = member or ctx.author

        stats = load_stats()
        uid = str(target.id)
        rec = ensure_user(stats, uid)

        total, win, lose = rec["참여"], rec["승리"], rec["패배"]
        rate = round(win / total * 100, 2) if total else 0.0

        riot_id = extract_riot_id(target.display_name)
        fow_url = None
        if riot_id:
            encoded = urllib.parse.quote(riot_id, safe="")
            fow_url = f"https://fow.lol/find/{encoded}"

        embed = discord.Embed(title=f"{target.display_name} 전적", color=0x2F3136)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="참여", value=f"{total}회", inline=True)
        embed.add_field(name="승", value=f"{win}", inline=True)
        embed.add_field(name="패", value=f"{lose}", inline=True)
        embed.add_field(name="승률", value=f"{rate}%", inline=True)

        if fow_url:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="FOW.LOL에서 전적 확인하기", url=fow_url, emoji="🖱️"))
            embed.set_footer(text="아래 버튼을 클릭하여 FOW.LOL에서 자세한 전적을 확인하세요.")
            await ctx.send(embed=embed, view=view)
        else:
            embed.set_footer(text="닉네임에서 '소환사명#태그'를 찾지 못했습니다.")
            await ctx.send(embed=embed)

    @commands.command(name="내전랭킹")
    async def rank_command(self, ctx: commands.Context):
        stats = load_stats()
        members = [(int(uid), data) for uid, data in stats.items() if data.get("참여", 0) >= 20]

        if not members:
            await ctx.send(embed=discord.Embed(
                title="내전랭킹",
                description="참여 20회 이상 유저가 없습니다.",
                color=0x2F3136
            ))
            return

        sorted_list = sorted(
            members,
            key=lambda x: (x[1]["승리"] / x[1]["참여"]) if x[1]["참여"] else 0,
            reverse=True
        )
        top10 = sorted_list[:20]

        embed = discord.Embed(title="승률 TOP 20 (참여 20회 이상)", color=0x2F3136)
        for idx, (uid, data) in enumerate(top10, 1):
            member = ctx.guild.get_member(uid)
            if not member:
                continue
            winrate = round(data["승리"] / data["참여"] * 100, 2)
            embed.add_field(
                name=f"{idx}. {member.display_name}",
                value=f"승률: {winrate}%\n참여: {data['참여']}전 {data['승리']}승 {data['패배']}패",
                inline=False
            )
        await ctx.send(embed=embed)

    @commands.command(name="판수랭킹")
    async def count_command(self, ctx: commands.Context):
        stats = load_stats()
        members = [(int(uid), data) for uid, data in stats.items() if data.get("참여", 0) > 0]

        if not members:
            await ctx.send(embed=discord.Embed(
                title="판수 랭킹",
                description="참여한 유저가 없습니다.",
                color=0x2F3136
            ))
            return

        sorted_list = sorted(members, key=lambda x: x[1]["참여"], reverse=True)[:20]

        embed = discord.Embed(title="📊 내전 판수 랭킹 (Top 20)", color=discord.Color.red())
        for idx, (uid, data) in enumerate(sorted_list, 1):
            member = ctx.guild.get_member(uid)
            if not member:
                continue
            embed.add_field(
                name=f"{idx}. {member.display_name}",
                value=f"{data['참여']}전 ({data['승리']}승 / {data['패배']}패)",
                inline=False
            )
        await ctx.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))
