import os
import discord
from discord import app_commands
from discord.ext import commands

BOT_TOKEN = os.getenv("BOT_TOKEN")
LEAKER_ROLE_ID = int(os.getenv("LEAKER_ROLE_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

intents = discord.Intents.default()
# This bot uses slash commands and forum starter messages; it does not need
# the privileged Message Content intent.

class SmileyBot(commands.Bot):
    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

bot = SmileyBot(command_prefix="!", intents=intents)

async def leaker_check(interaction: discord.Interaction) -> bool:
    if LEAKER_ROLE_ID and isinstance(interaction.user, discord.Member):
        if any(role.id == LEAKER_ROLE_ID for role in interaction.user.roles):
            return True
    if not interaction.response.is_done():
        await interaction.response.send_message(
            "You don't have permission to use this command.", ephemeral=True
        )
    return False

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")

async def get_forum_threads(forum: discord.ForumChannel, limit: int = 100):
    """Return active and recent archived posts, avoiding duplicate thread IDs."""
    found = {thread.id: thread for thread in forum.threads}
    try:
        async for thread in forum.archived_threads(limit=limit, oldest_first=False):
            found.setdefault(thread.id, thread)
    except (discord.Forbidden, discord.HTTPException):
        pass
    return list(found.values())

async def starter_message(thread: discord.Thread):
    try:
        return await thread.fetch_message(thread.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None

@bot.tree.command(name="add", description="Create a forum post for a resource with up to 10 files")
@app_commands.describe(
    name="Name of the resource / forum post title",
    creator="Creator name",
    forum="Forum channel where the post should be created",
    file1="File 1 (optional)", file2="File 2 (optional)",
    file3="File 3 (optional)", file4="File 4 (optional)",
    file5="File 5 (optional)", file6="File 6 (optional)",
    file7="File 7 (optional)", file8="File 8 (optional)",
    file9="File 9 (optional)", file10="File 10 (optional)",
    link="Optional link to the resource",
    description="Optional details about the resource"
)
@app_commands.check(leaker_check)
async def add_resource(
    interaction: discord.Interaction,
    name: str,
    creator: str,
    forum: discord.ForumChannel,
    file1: discord.Attachment | None = None,
    file2: discord.Attachment | None = None,
    file3: discord.Attachment | None = None,
    file4: discord.Attachment | None = None,
    file5: discord.Attachment | None = None,
    file6: discord.Attachment | None = None,
    file7: discord.Attachment | None = None,
    file8: discord.Attachment | None = None,
    file9: discord.Attachment | None = None,
    file10: discord.Attachment | None = None,
    link: str | None = None,
    description: str | None = None,
):
    attachments = [a for a in (file1, file2, file3, file4, file5, file6, file7, file8, file9, file10) if a]
    if not attachments and not (link and link.strip()):
        await interaction.response.send_message(
            "Attach at least one file or provide a link.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    embed = discord.Embed(
        title=name[:256],
        description=(description.strip() if description and description.strip() else "No description provided.")[:4096],
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Creator", value=creator[:1024], inline=True)
    embed.add_field(name="Added by", value=interaction.user.mention, inline=True)
    if link and link.strip():
        embed.add_field(name="Resource link", value=link.strip()[:1024], inline=False)

    files_to_send = []
    try:
        for attachment in attachments:
            files_to_send.append(await attachment.to_file())
        created = await forum.create_thread(
            name=name[:100],
            embeds=[embed],
            files=files_to_send,
            allowed_mentions=discord.AllowedMentions.none(),
            reason=f"Resource added by {interaction.user} using /add",
        )
        thread = created.thread
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to create posts in that forum or upload attachments.", ephemeral=True
        )
        return
    except discord.HTTPException as exc:
        await interaction.followup.send(
            "Discord couldn't create the forum post. Check the combined upload size and the bot's forum permissions. "
            f"Details: {str(exc)[:500]}", ephemeral=True
        )
        return
    finally:
        for file_obj in files_to_send:
            try:
                file_obj.close()
            except Exception:
                pass

    await interaction.followup.send(
        f"Created [**{name}**]({thread.jump_url}) in {forum.mention} with {len(attachments)} file(s).",
        ephemeral=True,
    )

@bot.tree.command(name="search", description="Search resource posts in a forum")
@app_commands.describe(forum="Forum to search", query="Resource name, creator, or description")
@app_commands.check(leaker_check)
async def search_resources(interaction: discord.Interaction, forum: discord.ForumChannel, query: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    matches = []
    threads = await get_forum_threads(forum, limit=100)
    for thread in threads:
        title_text = thread.name or ""
        msg = await starter_message(thread)
        embed_text = ""
        if msg:
            embed_text += msg.content or ""
            for embed in msg.embeds:
                embed_text += f" {embed.title or ''} {embed.description or ''}"
                embed_text += " " + " ".join(f"{field.name} {field.value}" for field in embed.fields)
        if query.casefold() in f"{title_text} {embed_text}".casefold():
            matches.append(thread)
    if not matches:
        await interaction.followup.send("No matching resources found in that forum's active/recent archived posts.", ephemeral=True)
        return
    lines = [f"• **{thread.name}** — [open post]({thread.jump_url})" for thread in matches[:10]]
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="recent", description="Show recent resource posts in a forum")
@app_commands.describe(forum="Forum to view")
@app_commands.check(leaker_check)
async def recent_resources(interaction: discord.Interaction, forum: discord.ForumChannel):
    await interaction.response.defer(ephemeral=True, thinking=True)
    threads = await get_forum_threads(forum, limit=100)
    threads.sort(key=lambda thread: thread.id, reverse=True)
    if not threads:
        await interaction.followup.send("No resource posts found in that forum yet.", ephemeral=True)
        return
    lines = [f"• **{thread.name}** — [open post]({thread.jump_url})" for thread in threads[:8]]
    await interaction.followup.send("\n".join(lines), ephemeral=True)

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable.")
if not LEAKER_ROLE_ID:
    raise RuntimeError("Missing or invalid LEAKER_ROLE_ID environment variable.")

bot.run(BOT_TOKEN)

