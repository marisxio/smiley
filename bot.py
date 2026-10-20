import os
import discord
from discord import app_commands
from discord.ext import commands

BOT_TOKEN = os.getenv("BOT_TOKEN")
LEAKER_ROLE_ID = int(os.getenv("LEAKER_ROLE_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

intents = discord.Intents.default()
intents.message_content = True

class SmileyBot(commands.Bot):
    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

bot = SmileyBot(command_prefix="!", intents=intents)

def has_leaker_role(interaction: discord.Interaction) -> bool:
    if not LEAKER_ROLE_ID or not isinstance(interaction.user, discord.Member):
        return False
    return any(role.id == LEAKER_ROLE_ID for role in interaction.user.roles)

async def leaker_check(interaction: discord.Interaction) -> bool:
    if has_leaker_role(interaction):
        return True
    await interaction.response.send_message(
        "You don't have permission to use this command.", ephemeral=True
    )
    return False

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")

@bot.tree.command(name="add", description="Add a resource with up to 10 files")
@app_commands.describe(
    name="Name of the resource",
    creator="Creator name",
    channel="Channel where the resource should be posted",
    file1="File 1 (optional)", file2="File 2 (optional)",
    file3="File 3 (optional)", file4="File 4 (optional)",
    file5="File 5 (optional)", file6="File 6 (optional)",
    file7="File 7 (optional)", file8="File 8 (optional)",
    file9="File 9 (optional)", file10="File 10 (optional)",
    link="Optional resource link", description="Optional resource details"
)
@app_commands.check(leaker_check)
async def add_resource(
    interaction: discord.Interaction,
    name: str,
    creator: str,
    channel: discord.TextChannel,
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
    attachments = [a for a in (
        file1, file2, file3, file4, file5,
        file6, file7, file8, file9, file10
    ) if a is not None]

    if not attachments and not (link and link.strip()):
        await interaction.response.send_message(
            "Please attach at least one file or provide a link.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title=name[:256],
        description=(description or "No description provided.")[:4096],
        color=discord.Color.blurple()
    )
    embed.add_field(name="Creator", value=creator[:1024], inline=True)
    embed.add_field(name="Added by", value=interaction.user.mention, inline=True)
    if link and link.strip():
        embed.add_field(name="Resource link", value=link.strip()[:1024], inline=False)

    await interaction.response.defer(ephemeral=True, thinking=True)
    files_to_send = []
    try:
        for attachment in attachments:
            files_to_send.append(await attachment.to_file())
        await channel.send(
            embed=embed,
            files=files_to_send,
            allowed_mentions=discord.AllowedMentions.none()
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I can't post in that channel. Check that I have permission to view the channel, send messages, attach files, and embed links.",
            ephemeral=True
        )
        return
    except discord.HTTPException as exc:
        await interaction.followup.send(
            "Discord couldn't accept the upload. Check the file sizes and the bot's channel permissions. "
            f"Details: {str(exc)[:500]}", ephemeral=True
        )
        return
    finally:
        for file_obj in files_to_send:
            try:
                file_obj.close()
            except Exception:
                pass

    count = len(attachments)
    await interaction.followup.send(
        f"Added **{name}** by **{creator}** to {channel.mention} with {count} file{'s' if count != 1 else ''}.",
        ephemeral=True
    )

@bot.tree.command(name="search", description="Search resource posts in a channel")
@app_commands.describe(channel="Channel to search", query="Resource name, creator, or description")
@app_commands.check(leaker_check)
async def search_resources(interaction: discord.Interaction, channel: discord.TextChannel, query: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    matches = []
    try:
        async for message in channel.history(limit=500):
            for embed in message.embeds:
                fields_text = " ".join(f"{field.name} {field.value}" for field in embed.fields)
                haystack = f"{embed.title or ''} {embed.description or ''} {fields_text}".casefold()
                if query.casefold() in haystack:
                    matches.append(message)
                    break
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to read that channel's history.", ephemeral=True)
        return
    if not matches:
        await interaction.followup.send("No matching resources found in that channel.", ephemeral=True)
        return
    lines = []
    for message in matches[:10]:
        title = message.embeds[0].title if message.embeds else "Resource"
        lines.append(f"• **{title or 'Resource'}** — [jump to post]({message.jump_url})")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="recent", description="Show recent resource posts in a channel")
@app_commands.describe(channel="Channel to view")
@app_commands.check(leaker_check)
async def recent_resources(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True, thinking=True)
    lines = []
    try:
        async for message in channel.history(limit=100):
            if message.embeds:
                title = message.embeds[0].title or "Resource"
                lines.append(f"• **{title}** — [jump to post]({message.jump_url})")
            if len(lines) >= 8:
                break
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to read that channel's history.", ephemeral=True)
        return
    if not lines:
        await interaction.followup.send("No resource posts found in that channel yet.", ephemeral=True)
        return
    await interaction.followup.send("\n".join(lines), ephemeral=True)

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable.")
if not LEAKER_ROLE_ID:
    raise RuntimeError("Missing or invalid LEAKER_ROLE_ID environment variable.")

bot.run(BOT_TOKEN)
