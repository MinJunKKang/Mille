# cogs/moderation_cog.py
import json
import discord
from discord.ext import commands
from typing import Dict, Optional, Set

class ModerationCog(commands.Cog):
    """욕설 필터, 스팸 단어 관리, 청소 등"""
    def __init__(self, bot: commands.Bot, role_ids: Optional[Dict[str, int]] = None):
        self.bot = bot
        self.role_ids: Set[int] = set(role_ids.values()) if role_ids else set()

    # ---- 유틸 ----
    def _has_cleanup_power(self, member: discord.Member) -> bool:
        role_ids = {r.id for r in member.roles}
        return bool(role_ids & self.role_ids) or member.guild_permissions.administrator

    def load_bad_words(self):
        try:
            with open("bad_words.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("bad_words", [])
        except (FileNotFoundError, json.JSONDecodeError):
            with open("bad_words.json", "w", encoding="utf-8") as f:
                json.dump({"bad_words": []}, f, ensure_ascii=False, indent=4)
            return []

    # ---- 리스너: 욕설 필터 ----
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        content = message.content.strip()
        lower = content.lower()
        prefix = "!"

        # 1) 명령 메시지는 필터 대상에서 제외 (여기서는 process_commands 호출 안 함)
        if lower.startswith(prefix):
            return

        # 2) 일반 메시지에만 욕설 필터 적용
        bad_words = set(w.strip().lower() for w in self.load_bad_words())
        words = set(lower.split())
        if bad_words & words:
            role_titles = {"지우": "지우군", "빛나": "빛나양"}
            title = message.author.display_name
            for role in message.author.roles:
                if role.name in role_titles:
                    title = role_titles[role.name]
                    break

            await message.channel.send(
                f"{message.author.mention} \n{title} 말 좀 예뿌게 하세요~ <:57:1357677118028517488>"
            )

    # ---- 스팸 단어 추가/삭제 ----
    @commands.command(name="스팸추가")
    @commands.has_permissions(administrator=True)
    async def add_bad_word(self, ctx: commands.Context, *, word: str):
        bad_words = self.load_bad_words()
        word = word.strip().lower()

        if word in [w.strip().lower() for w in bad_words]:
            await ctx.send("이미 등록된 단어입니다.")
            return

        bad_words.append(word)
        with open("bad_words.json", "w", encoding="utf-8") as f:
            json.dump({"bad_words": bad_words}, f, ensure_ascii=False, indent=4)
        await ctx.send(f"`{word}` 추가 완료")

    @commands.command(name="스팸삭제")
    @commands.has_permissions(administrator=True)
    async def remove_bad_word(self, ctx: commands.Context, *, word: str):
        bad_words = self.load_bad_words()
        word = word.strip().lower()
        bad_words_lower = [w.strip().lower() for w in bad_words]

        if word not in bad_words_lower:
            await ctx.send("등록되지 않은 단어입니다.")
            return

        idx = bad_words_lower.index(word)
        removed = bad_words[idx]
        bad_words.pop(idx)

        with open("bad_words.json", "w", encoding="utf-8") as f:
            json.dump({"bad_words": bad_words}, f, ensure_ascii=False, indent=4)
        await ctx.send(f"`{removed}` 삭제 완료")

    # ---- 청소 ----
    class ConfirmCleanView(discord.ui.View):
        def __init__(self, parent: "ModerationCog", ctx: commands.Context, amount: int, *, timeout: float = 30):
            super().__init__(timeout=timeout)
            self.parent = parent
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

            perms = self.ctx.channel.permissions_for(self.ctx.me)
            if not perms.manage_messages:
                await interaction.response.send_message("❌ 봇에 **메시지 관리** 권한이 없습니다.", ephemeral=True)
                return

            await interaction.response.send_message("삭제를 시작합니다…", ephemeral=True)
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                pass
            try:
                await self.ctx.message.delete()
            except discord.HTTPException:
                pass

            try:
                deleted = await self.ctx.channel.purge(limit=self.amount)
                await interaction.followup.send(f"✅ {len(deleted)}개의 메시지를 삭제했습니다.", ephemeral=True)
            except discord.Forbidden:
                await interaction.followup.send("❌ 삭제 중 권한 오류가 발생했습니다.", ephemeral=True)
            except discord.HTTPException as e:
                await interaction.followup.send(f"❌ 삭제 중 오류: {e}", ephemeral=True)

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

    @commands.command(name="청소")
    async def clean(self, ctx: commands.Context, amount: int):
        if not self._has_cleanup_power(ctx.author):
            try:
                await ctx.author.send("이 명령어를 사용할 권한이 없습니다.")
            except discord.Forbidden:
                await ctx.reply("이 명령어를 사용할 권한이 없습니다.", delete_after=4)
            return

        if not (1 <= amount <= 500):
            try:
                await ctx.author.send("1 ~ 500 사이의 숫자를 입력해주세요.")
            except discord.Forbidden:
                await ctx.reply("1 ~ 500 사이의 숫자를 입력해주세요.", delete_after=4)
            return

        embed = discord.Embed(
            title="정말로 지우시겠습니까?",
            description=f"이 채널에서 최근 **{amount}개**의 메시지가 삭제됩니다.",
            color=discord.Color.red()
        )
        view = ModerationCog.ConfirmCleanView(self, ctx, amount)
        prompt = await ctx.send(embed=embed, view=view)

        async def _cleanup_when_timeout():
            await view.wait()
            if prompt and prompt.channel and any(i for i in view.children):
                try:
                    await prompt.delete()
                except discord.HTTPException:
                    pass
        self.bot.loop.create_task(_cleanup_when_timeout())

    @clean.error
    async def clean_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingRequiredArgument):
            try:
                await ctx.author.send("사용법: `!청소 <1~500>`")
            except discord.Forbidden:
                await ctx.reply("사용법: `!청소 <1~500>`", delete_after=4)
