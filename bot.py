import os
import sqlite3
import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.getenv("BOT_TOKEN")
ROLE_ID = int(os.environ["LEAKER_ROLE_ID"])
GUILD_ID = os.getenv("GUILD_ID")
GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None
if not TOKEN:
    raise RuntimeError("Set BOT_TOKEN in Railway Variables.")

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
# Required to inspect message text for X links. Also enable Message Content Intent
# for this bot in the Discord Developer Portal.
intents.message_content = True

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

# Persist counts in SQLite. On Railway, mount a Volume at /data and set
# LEAK_DB_PATH=/data/leaks.sqlite3 so the database survives redeploys.
DB_PATH = os.getenv("LEAK_DB_PATH", "leaks.sqlite3")

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS leaks (message_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, created_at TEXT NOT NULL, source TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS monthly_resets (guild_id TEXT PRIMARY KEY, reset_at TEXT NOT NULL, reset_by TEXT NOT NULL, new_payout_policy INTEGER NOT NULL DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS x_accounts (guild_id TEXT NOT NULL, username TEXT NOT NULL, x_user_id TEXT NOT NULL, channel_id TEXT NOT NULL, added_at TEXT NOT NULL, added_by TEXT NOT NULL, PRIMARY KEY (guild_id, username))")
    conn.execute("CREATE TABLE IF NOT EXISTS x_seen_posts (tweet_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, username TEXT NOT NULL, channel_id TEXT NOT NULL, created_at TEXT NOT NULL)")
    # Upgrade databases created by earlier bot versions without affecting the in-progress period.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(monthly_resets)").fetchall()}
    if "new_payout_policy" not in columns:
        conn.execute("ALTER TABLE monthly_resets ADD COLUMN new_payout_policy INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    return conn

def record_leak(message_id: int, user_id: int, guild_id: int, channel_id: int, created_at: datetime, source: str = "live") -> bool:
    """Record each resource message once. Returns True only for a new record."""
    conn = db_connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO leaks(message_id,user_id,guild_id,channel_id,created_at,source) VALUES(?,?,?,?,?,?)",
            (str(message_id), str(user_id), str(guild_id), str(channel_id), created_at.astimezone(timezone.utc).isoformat(), source),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()

def month_bounds(now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.isoformat(), end.isoformat()

def effective_month_start(guild_id: int) -> tuple[str, str]:
    """Return the active payout-period bounds for this server.

    Before the first manual reset, the tally follows the calendar month. Once
    the owner runs /reset_month, the new period starts at that timestamp and
    continues across calendar-month boundaries until the owner resets again.
    """
    calendar_start, calendar_end = month_bounds()
    conn = db_connect()
    try:
        row = conn.execute("SELECT reset_at FROM monthly_resets WHERE guild_id=?", (str(guild_id),)).fetchone()
        if row:
            return row[0], "9999-12-31T23:59:59+00:00"
        return calendar_start, calendar_end
    finally:
        conn.close()

def leak_count(user_id: int, *, monthly: bool = True, guild_id: int | None = None) -> int:
    conn = db_connect()
    try:
        if monthly:
            if guild_id is not None:
                start, end = effective_month_start(guild_id)
            else:
                start, end = month_bounds()
            row = conn.execute("SELECT COUNT(*) FROM leaks WHERE user_id=? AND created_at>=? AND created_at<?", (str(user_id), start, end)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM leaks WHERE user_id=?", (str(user_id),)).fetchone()
        return int(row[0])
    finally:
        conn.close()

MAX_PERIOD_PAYOUT = 20
LEAKS_FOR_MAX_PAYOUT = 40  # $5 per full 10 leaks, capped at $20

def new_payout_policy_active(guild_id: int | None) -> bool:
    """The new $20 cap becomes active only after the owner starts a new period.

    Until then, the in-progress period keeps the previous uncapped $5 per 10
    leaks rule. The reset timestamp is persisted in SQLite.
    """
    if guild_id is None:
        return False
    conn = db_connect()
    try:
        row = conn.execute("SELECT new_payout_policy FROM monthly_resets WHERE guild_id=?", (str(guild_id),)).fetchone()
        return bool(row and row[0])
    finally:
        conn.close()

def estimated_payment(count: int, *, capped: bool = True) -> int:
    """$5 per complete 10 leaks; apply the $20 cap only for activated periods."""
    amount = (count // 10) * 5
    return min(amount, MAX_PERIOD_PAYOUT) if capped else amount

def is_staff(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator)

def user_id_from_embed(message: discord.Message) -> int | None:
    # /add posts are sent by the bot, so historical ownership is stored in the embed.
    # Older human-authored resource messages fall back to message.author below.
    for embed in message.embeds:
        for field in embed.fields:
            if field.name.casefold().strip() in {"leaked by", "added by"}:
                import re
                match = re.search(r"<@!?(\d{15,22})>", field.value)
                if match:
                    return int(match.group(1))
    if message.author and not message.author.bot:
        return message.author.id
    return None

def looks_like_resource(message: discord.Message) -> bool:
    # Resource messages use an embed title and a Creator field. This avoids counting
    # the initial creator-thread starter message as a leak.
    return any(e.title and any(f.name.casefold().strip() == "creator" for f in e.fields) for e in message.embeds)


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
    """Fetch active threads from Discord, plus archived threads where accessible.

    The channel's local ``forum.threads`` cache is not guaranteed to contain every
    active post. Looking only there can make /add mistakenly create duplicates.
    """
    threads = {thread.id: thread for thread in forum.threads}
    try:
        active_threads = await forum.guild.active_threads()
        for thread in active_threads:
            if thread.parent_id == forum.id:
                threads[thread.id] = thread
    except (AttributeError, discord.Forbidden, discord.HTTPException) as exc:
        print(f"Could not fetch all active threads for #{forum.name}: {exc}")

    if include_archived:
        try:
            async for thread in forum.archived_threads(limit=None):
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

        resource_message = await thread.send(embed=embed, files=files, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        record_leak(resource_message.id, interaction.user.id, interaction.guild_id, channel.id, resource_message.created_at, "live")
        status = "Created a creator post and added the resource" if created else "Added the resource to the existing creator post"
        current_count = leak_count(interaction.user.id, monthly=True, guild_id=interaction.guild_id)
        confirmation = f"{status}: **{discord.utils.escape_markdown(creator)}** in {channel.mention}. [Open creator post]({thread.jump_url})"
        if new_payout_policy_active(interaction.guild_id) and current_count >= LEAKS_FOR_MAX_PAYOUT:
            confirmation += (
                "\n\n⚠️ **Monthly leaking credit warning**\n"
                "You've reached the 40 leaks needed for the maximum **$20** payout this period. "
                "You can continue adding resources, but your payout will not increase above $20. "
                "If you wish to continue, run `/add` again; otherwise, you can stop for this period."
            )
        await interaction.followup.send(confirmation, ephemeral=True)
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
@app_commands.describe(query="Search creator names, resource names, descriptions, or links", channel="Forum to search; defaults to this forum")
async def search(interaction: discord.Interaction, query: str, channel: discord.ForumChannel | None = None):
    if not await role_check(interaction):
        return
    target = channel or interaction.channel
    if not isinstance(target, discord.ForumChannel):
        await interaction.response.send_message("Choose a forum channel.", ephemeral=False)
        return
    await interaction.response.defer(ephemeral=False, thinking=True)
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
        await interaction.followup.send("I can't read this forum. Check View Channel and Read Message History permissions.", ephemeral=False)
        return
    if not found:
        await interaction.followup.send(f"No matches found in {target.mention}. I checked accessible active and recent archived creator posts.", ephemeral=False)
        return
    lines = [f"• **{discord.utils.escape_markdown(resource)}** in **{discord.utils.escape_markdown(thread.name)}** — [open post]({thread.jump_url})" for thread, resource in found]
    embed = discord.Embed(title=f"Search results: {query[:180]}", description="\n".join(lines), color=discord.Color.blurple())
    embed.set_footer(text=f"Forum: #{target.name} · up to 8 results")
    await interaction.followup.send(embed=embed, ephemeral=False)


@bot.tree.command(name="creator", description="Find creator posts by creator name.")
@app_commands.describe(name="Full or partial creator name", channel="Optional forum; otherwise searches both resource forums")
async def creator(interaction: discord.Interaction, name: str, channel: discord.ForumChannel | None = None):
    if not await role_check(interaction):
        return
    if not interaction.guild:
        await interaction.response.send_message("Use this command inside your server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    if channel:
        forums = [channel]
    else:
        configured = [x.strip() for x in os.getenv("FORUM_CHANNEL_IDS", "").split(",") if x.strip()]
        if configured:
            forums = [interaction.guild.get_channel(int(cid)) for cid in configured]
            forums = [f for f in forums if isinstance(f, discord.ForumChannel)]
        else:
            names = {"after-effects", "mobile-resources"}
            forums = [c for c in interaction.guild.channels if isinstance(c, discord.ForumChannel) and c.name.casefold() in names]
    if not forums:
        await interaction.followup.send("I couldn't find the resource forums. Set FORUM_CHANNEL_IDS in Railway or check the forum names.", ephemeral=True)
        return
    needle = name.strip().casefold()
    matches = []
    for forum in forums:
        try:
            for thread in await get_forum_threads(forum):
                if needle in thread.name.casefold():
                    matches.append((thread, forum))
        except (discord.Forbidden, discord.HTTPException):
            continue
    matches.sort(key=lambda item: item[0].last_message_id or item[0].id, reverse=True)
    if not matches:
        await interaction.followup.send(f"No creator names matching **{discord.utils.escape_markdown(name)}** were found.", ephemeral=True)
        return
    lines = [f"• **{discord.utils.escape_markdown(thread.name)}** · #{forum.name} — [open creator post]({thread.jump_url})" for thread, forum in matches[:10]]
    embed = discord.Embed(title=f"Creator search: {name[:180]}", description="\n".join(lines), color=discord.Color.blurple())
    embed.set_footer(text=f"Showing up to 10 of {len(matches)} matches")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="latest", description="Show the most recently added resource leak.")
@app_commands.describe(channel="Optional forum; otherwise checks both resource forums")
async def latest(interaction: discord.Interaction, channel: discord.ForumChannel | None = None):
    if not await role_check(interaction):
        return
    if not interaction.guild:
        await interaction.response.send_message("Use this command inside your server.", ephemeral=False)
        return
    await interaction.response.defer(ephemeral=False, thinking=True)
    if channel:
        forums = [channel]
    else:
        configured = [x.strip() for x in os.getenv("FORUM_CHANNEL_IDS", "").split(",") if x.strip()]
        if configured:
            forums = [interaction.guild.get_channel(int(cid)) for cid in configured]
            forums = [f for f in forums if isinstance(f, discord.ForumChannel)]
        else:
            names = {"after-effects", "mobile-resources"}
            forums = [c for c in interaction.guild.channels if isinstance(c, discord.ForumChannel) and c.name.casefold() in names]
    if not forums:
        await interaction.followup.send("I couldn't find the resource forums. Set FORUM_CHANNEL_IDS in Railway or check the forum names.", ephemeral=False)
        return
    newest = None
    failures = []
    for forum in forums:
        try:
            threads = await get_forum_threads(forum)
        except (discord.Forbidden, discord.HTTPException):
            failures.append(forum.name)
            continue
        for thread in threads:
            try:
                async for message in thread.history(limit=100, oldest_first=False):
                    if looks_like_resource(message):
                        if newest is None or message.created_at > newest[0].created_at:
                            newest = (message, thread, forum)
                        break
            except (discord.Forbidden, discord.HTTPException):
                continue
    if newest is None:
        extra = f" I couldn't read: {', '.join(failures)}." if failures else ""
        await interaction.followup.send("I couldn't find any accessible resource posts." + extra, ephemeral=False)
        return
    message, thread, forum = newest
    resource_name = next((e.title for e in message.embeds if e.title), "Untitled resource")
    creator_name = next((f.value for e in message.embeds for f in e.fields if f.name.casefold().strip() == "creator"), thread.name)
    embed = discord.Embed(title="Most recent leak", description=f"**Resource:** {discord.utils.escape_markdown(resource_name)}\n**Creator:** {discord.utils.escape_markdown(creator_name)}\n**Forum:** {forum.mention}\n**Added:** <t:{int(message.created_at.timestamp())}:R>\n[Open resource message]({message.jump_url})", color=discord.Color.blurple())
    await interaction.followup.send(embed=embed, ephemeral=False)


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


@bot.tree.command(name="leaks", description="View a leaker's leak count for this month.")
@app_commands.describe(user="Leaker to check; defaults to yourself")
async def leaks(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user
    if user and user.id != interaction.user.id and not is_staff(interaction):
        await interaction.response.send_message("Only staff can view another member's leak count.", ephemeral=True)
        return
    count = leak_count(target.id, monthly=True, guild_id=interaction.guild_id)
    total = leak_count(target.id, monthly=False)
    embed = discord.Embed(title=f"♡ Leak count · {target.display_name}", color=discord.Color.blurple())
    embed.add_field(name="This month", value=f"**{count}** leaks", inline=True)
    capped = new_payout_policy_active(interaction.guild_id)
    embed.add_field(name="Estimated payment", value=f"**${estimated_payment(count, capped=capped)}**", inline=True)
    embed.add_field(name="All recorded time", value=f"**{total}** leaks", inline=True)
    footer = "$5 per 10 leaks · maximum $20 per payout period" if capped else "$5 per 10 leaks · current period keeps the previous payout rules until /reset_month"
    embed.set_footer(text=footer + " · all-time count is uncapped")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="leakerboard", description="Show this month's top leakers.")
async def leakerboard(interaction: discord.Interaction):
    if not interaction.guild_id:
        await interaction.response.send_message("Use this command in your server.", ephemeral=True)
        return
    start, end = effective_month_start(interaction.guild_id)
    conn = db_connect()
    try:
        rows = conn.execute("SELECT user_id, COUNT(*) AS n FROM leaks WHERE guild_id=? AND created_at>=? AND created_at<? GROUP BY user_id ORDER BY n DESC LIMIT 10", (str(interaction.guild_id), start, end)).fetchall()
    finally:
        conn.close()
    if not rows:
        await interaction.response.send_message("No leaks have been counted this month yet.", ephemeral=True)
        return
    lines = []
    capped = new_payout_policy_active(interaction.guild_id)
    for rank, (uid, count) in enumerate(rows, start=1):
        lines.append(f"**{rank}.** <@{uid}> — **{count}** leaks · ${estimated_payment(int(count), capped=capped)}")
    embed = discord.Embed(title="୨୧ Monthly leakerboard", description="\n".join(lines), color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="reset_month", description="Server owner only: start a fresh leak-count and payout period.")
async def reset_month(interaction: discord.Interaction):
    if not interaction.guild or not interaction.guild_id:
        await interaction.response.send_message("Use this command inside your server.", ephemeral=True)
        return
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("Only the server owner can reset the monthly leak counts.", ephemeral=True)
        return

    reset_at = datetime.now(timezone.utc).isoformat()
    conn = db_connect()
    try:
        conn.execute(
            "INSERT INTO monthly_resets(guild_id, reset_at, reset_by, new_payout_policy) VALUES(?,?,?,1) "
            "ON CONFLICT(guild_id) DO UPDATE SET reset_at=excluded.reset_at, reset_by=excluded.reset_by, new_payout_policy=1",
            (str(interaction.guild_id), reset_at, str(interaction.user.id)),
        )
        conn.commit()
    finally:
        conn.close()

    await interaction.response.send_message(
        "୨୧ Fresh payout period started! The new system is now active: **$5 per 10 leaks, capped at $20 per period (maximum reached at 40 leaks)**. Everyone's period count and estimated payout start at 0 from this point; the leaderboard will build from new-period leaks. All-time records are preserved. Run `/reset_month` again when you're ready to begin another period.",
        ephemeral=True,
    )


@bot.tree.command(name="backfill_leaks", description="Staff: scan existing resource replies in both resource forums.")
@app_commands.describe(channel="Optional: scan only one forum; otherwise scans both known resource forums")
async def backfill_leaks(interaction: discord.Interaction, channel: discord.ForumChannel | None = None):
    if not is_staff(interaction):
        await interaction.response.send_message("This is a staff-only command (Manage Server required).", ephemeral=True)
        return
    if not interaction.guild:
        await interaction.response.send_message("Use this command inside your server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    if channel:
        forums = [channel]
    else:
        # Preferred: set FORUM_CHANNEL_IDS=123,456 in Railway. If unset, use names.
        configured = [x.strip() for x in os.getenv("FORUM_CHANNEL_IDS", "").split(",") if x.strip()]
        if configured:
            forums = [interaction.guild.get_channel(int(cid)) for cid in configured]
            forums = [f for f in forums if isinstance(f, discord.ForumChannel)]
        else:
            names = {"after-effects", "mobile-resources"}
            forums = [c for c in interaction.guild.channels if isinstance(c, discord.ForumChannel) and c.name.casefold() in names]
    if not forums:
        await interaction.followup.send("I couldn't find the forums. Set FORUM_CHANNEL_IDS to the two forum channel IDs in Railway, or check the channel names.", ephemeral=True)
        return
    scanned = added = skipped = 0
    failures = []
    for forum in forums:
        try:
            threads = await get_forum_threads(forum, include_archived=True)
        except (discord.Forbidden, discord.HTTPException) as exc:
            failures.append(f"#{forum.name}: couldn't list posts ({type(exc).__name__})")
            continue
        for thread in threads:
            try:
                # oldest_first doesn't matter because each Discord message ID is unique in SQLite.
                async for message in thread.history(limit=None, oldest_first=True):
                    if not looks_like_resource(message):
                        continue
                    scanned += 1
                    submitter_id = user_id_from_embed(message)
                    if submitter_id is None:
                        skipped += 1
                        continue
                    if record_leak(message.id, submitter_id, interaction.guild_id, forum.id, message.created_at, "backfill"):
                        added += 1
            except (discord.Forbidden, discord.HTTPException) as exc:
                failures.append(f"{thread.name}: couldn't read replies ({type(exc).__name__})")
    msg = f"**Historical scan finished**\nForums scanned: {len(forums)}\nResource messages checked: {scanned}\nNew leak records added: {added}\nAlready counted / duplicates ignored: {scanned - added - skipped}\nCould not identify a submitter: {skipped}"
    if failures:
        msg += "\n\n**Some history was inaccessible:**\n" + "\n".join(f"• {x}" for x in failures[:8])
        msg += "\nCheck View Channel, Read Message History, and access to archived posts; then rerun safely."
    msg += "\n\nYou can rerun this command; existing message IDs won't be counted twice."
    await interaction.followup.send(msg, ephemeral=True)


# Automatic X-link styling (no X API token or credits required).
# Set X_LINK_CHANNEL_ID in Railway to the one channel where links should be styled.
X_LINK_CHANNEL_ID = os.getenv("X_LINK_CHANNEL_ID", "").strip()
X_LINK_RE = re.compile(r"https?://(?:www\.)?(?:x\.com|twitter\.com)/[^\s<>]+", re.IGNORECASE)

@bot.event
async def on_message(message: discord.Message):
    # Ignore DMs, other channels, and messages sent by bots to prevent loops.
    if message.author.bot or not message.guild or not X_LINK_CHANNEL_ID:
        return
    if str(message.channel.id) != X_LINK_CHANNEL_ID:
        return

    matches = X_LINK_RE.findall(message.content or "")
    if not matches:
        return

    # Trim common trailing punctuation that is not part of the URL.
    link = matches[0].rstrip(".,!?;:)]}")
    embed = discord.Embed(
        title="୨୧  X link shared  ୨୧",
        description=f"♡ [Open the original post]({link}) ♡\n\n୨୧・thank you for sharing!・୨୧",
        color=discord.Color.from_rgb(255, 170, 205),
    )
    embed.set_footer(text="Smiley · link preview")
    try:
        await message.channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden:
        print(f"Missing permission to send embeds in X link channel {message.channel.id}")
    except discord.HTTPException as exc:
        print(f"Could not format X link in channel {message.channel.id}: {exc}")

    # Keep normal application commands working after adding on_message.
    await bot.process_commands(message)

bot.run(TOKEN)
