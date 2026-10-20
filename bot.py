import os
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("BOT_TOKEN")
ROLE_ID = int(os.environ["LEAKER_ROLE_ID"])
GUILD_ID = os.getenv("GUILD_ID")
GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None
if not TOKEN:
    raise RuntimeError("Set BOT_TOKEN in Railway Variables.")

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True

class ResourceBot(commands.Bot):
    async def setup_hook(self):
        if GUILD:
            self.tree.copy_global_to(guild=GUILD)
            await self.tree.sync(guild=GUILD)
            print(f"Commands synced to server {GUILD_ID}")
        else:
            await self.tree.sync()
            print("Global commands synced; they can take longer to appear.")

bot = ResourceBot(command_prefix="!", intents=intents)


def has_role(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and any(r.id == ROLE_ID for r in interaction.user.roles)


async def role_check(interaction: discord.Interaction) -> bool:
    if not has_role(interaction):
        message = "You need the approved leaker role to use this command."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False
    return True


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


async def get_forum_threads(forum: discord.ForumChannel, *, include_archived: bool = True):
    """Return active threads and, when possible, archived threads from a forum."""
    threads = {thread.id: thread for thread in forum.threads}
    if include_archived:
        try:
            async for thread in forum.archived_threads(limit=100):
                threads[thread.id] = thread
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"Could not list archived threads in #{forum.name}: {exc}")
    return list(threads.values())


async def find_creator_thread(forum: discord.ForumChannel, creator: str):
    """Creator names are the forum post titles, so resources from all leakers group together."""
    threads = await get_forum_threads(forum)
    key = creator.strip().casefold()
    matches = [thread for thread in threads if thread.name.strip().casefold() == key]
    if not matches:
        return None
    # Prefer an active thread when duplicates already exist.
    matches.sort(key=lambda thread: thread.archived)
    return matches[0]


def make_resource_embed(name: str, creator: str, leaker: discord.abc.User, description: str | None, link: str | None):
    embed = discord.Embed(
        title=name[:256],
        description=(description or "No description provided.")[:4000],
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Creator", value=creator[:1024], inline=True)
    embed.add_field(name="Leaked by", value=f"{leaker.mention}", inline=True)
    if link:
        embed.add_field(name="Resource link", value=link[:1024], inline=False)
    embed.set_footer(text="Smiley Resource Library")
    return embed


@bot.tree.command(name="add", description="Add a resource inside the creator's forum post.")
@app_commands.describe(
    name="Resource name, such as Twixtor or CCs",
    creator="Creator name; resources with the same name go in the same post",
    channel="The forum to add this resource to",
    description="Optional details about this resource",
    link="Optional resource/source link",
    file1="Optional attachment 1", file2="Optional attachment 2", file3="Optional attachment 3",
    file4="Optional attachment 4", file5="Optional attachment 5", file6="Optional attachment 6",
    file7="Optional attachment 7", file8="Optional attachment 8", file9="Optional attachment 9",
    file10="Optional attachment 10",
)
async def add(
    interaction: discord.Interaction,
    name: str,
    creator: str,
    channel: discord.ForumChannel,
    description: str | None = None,
    link: str | None = None,
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
):
    if not await role_check(interaction):
        return
    attachments = [a for a in (file1, file2, file3, file4, file5, file6, file7, file8, file9, file10) if a]
    if not attachments and not link:
        await interaction.response.send_message("Attach at least one file or add a link.", ephemeral=True)
        return
    if interaction.guild_id != channel.guild.id:
        await interaction.response.send_message("Choose a forum from this server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    embed = make_resource_embed(name, creator, interaction.user, description, link)
    files = []
    try:
        for attachment in attachments:
            files.append(await attachment.to_file())
        thread = await find_creator_thread(channel, creator)
        created = False
        if thread is None:
            # A creator gets one forum post; its title is the creator's name.
            result = await channel.create_thread(
                name=creator.strip()[:100],
                content=f"**Creator resource collection for {creator.strip()}**\nNew resources from different leakers are added as replies in this post.",
                reason=f"Create creator resource collection for {creator.strip()}",
            )
            thread = result.thread
            created = True
        elif thread.archived or thread.locked:
            # The bot needs Manage Threads permission to reopen archived/locked posts.
            await thread.edit(archived=False, locked=False)

        await thread.send(embed=embed, files=files, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        status = "Created a creator post and added the resource" if created else "Added the resource to the existing creator post"
        await interaction.followup.send(f"{status}: **{discord.utils.escape_markdown(creator)}** in {channel.mention}. [Open creator post]({thread.jump_url})", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to create posts or send messages/files in that forum. Check Create Posts, Send Messages in Posts, Attach Files, Embed Links, Read Message History, and Manage Threads (for archived posts).", ephemeral=True)
    except discord.HTTPException as exc:
        await interaction.followup.send(f"Discord couldn't add this resource. Check attachment sizes and bot permissions, then try again. ({exc.status})", ephemeral=True)
    finally:
        for file in files:
            try:
                file.close()
            except Exception:
                pass


@bot.tree.command(name="search", description="Search creator posts and resources in a forum.")
@app_commands.describe(query="Words from creator, resource name, description, or link", channel="Forum to search; defaults to this forum")
async def search(interaction: discord.Interaction, query: str, channel: discord.ForumChannel | None = None):
    if not await role_check(interaction):
        return
    target = channel or interaction.channel
    if not isinstance(target, discord.ForumChannel):
        await interaction.response.send_message("Choose a forum channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    needle = query.casefold()
    found = []
    try:
        threads = await get_forum_threads(target)
        for thread in sorted(threads, key=lambda t: t.last_message_id or t.id, reverse=True):
            # Search the creator title, then a bounded number of resource replies.
            matched_resource = None
            if needle in thread.name.casefold():
                matched_resource = thread.name
            else:
                try:
                    async for message in thread.history(limit=100, oldest_first=False):
                        searchable = [message.content or ""]
                        for embed in message.embeds:
                            searchable.extend([embed.title or "", embed.description or ""])
                            searchable.extend(f"{field.name} {field.value}" for field in embed.fields)
                        if needle in " ".join(searchable).casefold():
                            matched_resource = next((e.title for e in message.embeds if e.title), thread.name)
                            break
                except (discord.Forbidden, discord.HTTPException):
                    continue
            if matched_resource:
                found.append((thread, matched_resource))
            if len(found) >= 8:
                break
    except discord.Forbidden:
        await interaction.followup.send("I can't read this forum. Check View Channel and Read Message History permissions.", ephemeral=True)
        return
    if not found:
        await interaction.followup.send(f"No matches found in {target.mention}. I checked accessible active and recent archived creator posts.", ephemeral=True)
        return
    lines = [f"• **{discord.utils.escape_markdown(resource)}** in **{discord.utils.escape_markdown(thread.name)}** — [open post]({thread.jump_url})" for thread, resource in found]
    embed = discord.Embed(title=f"Search results: {query[:180]}", description="\n".join(lines), color=discord.Color.blurple())
    embed.set_footer(text=f"Forum: #{target.name} · up to 8 results")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="recent", description="Show recently updated creator posts in a forum.")
@app_commands.describe(channel="Forum to check; defaults to this forum")
async def recent(interaction: discord.Interaction, channel: discord.ForumChannel | None = None):
    if not await role_check(interaction):
        return
    target = channel or interaction.channel
    if not isinstance(target, discord.ForumChannel):
        await interaction.response.send_message("Choose a forum channel.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        threads = await get_forum_threads(target)
    except discord.Forbidden:
        await interaction.followup.send("I can't read this forum. Check View Channel and Read Message History permissions.", ephemeral=True)
        return
    threads.sort(key=lambda t: t.last_message_id or t.id, reverse=True)
    if not threads:
        await interaction.followup.send(f"No creator posts found in {target.mention}.", ephemeral=True)
        return
    lines = [f"• **{discord.utils.escape_markdown(thread.name)}** — [open creator post]({thread.jump_url})" for thread in threads[:8]]
    embed = discord.Embed(title=f"Recent creator posts · #{target.name}", description="\n".join(lines), color=discord.Color.blurple())
    await interaction.followup.send(embed=embed, ephemeral=True)

bot.run(TOKEN)
