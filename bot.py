import os
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv('BOT_TOKEN')
ROLE_ID = int(os.environ['LEAKER_ROLE_ID'])
GUILD_ID = os.getenv('GUILD_ID')
GUILD = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None
if not TOKEN:
    raise RuntimeError('Set BOT_TOKEN in Railway Variables.')

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True

class ResourceBot(commands.Bot):
    async def setup_hook(self):
        if GUILD:
            self.tree.copy_global_to(guild=GUILD)
            await self.tree.sync(guild=GUILD)
            print(f'Commands synced to server {GUILD_ID}')
        else:
            await self.tree.sync()
            print('Global commands synced; they can take longer to appear.')

bot = ResourceBot(command_prefix='!', intents=intents)

def has_role(interaction):
    return isinstance(interaction.user, discord.Member) and any(r.id == ROLE_ID for r in interaction.user.roles)

async def role_check(interaction):
    if not has_role(interaction):
        await interaction.response.send_message('You need the approved leaker role to use this command.', ephemeral=True)
        return False
    return True

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} ({bot.user.id})')

@bot.tree.command(name='add', description='Post a resource in a selected channel.')
@app_commands.describe(name='Resource name', creator='Original creator name', channel='Channel to post in', file='Optional file attachment', link='Optional resource/source link', description='Optional details')
async def add(interaction: discord.Interaction, name: str, creator: str, channel: discord.TextChannel, file: discord.Attachment = None, link: str = None, description: str = None):
    if not await role_check(interaction):
        return
    if not file and not link:
        await interaction.response.send_message('Attach a file, add a link, or include both.', ephemeral=True)
        return
    if interaction.guild_id != channel.guild.id:
        await interaction.response.send_message('Choose a channel from this server.', ephemeral=True)
        return
    embed = discord.Embed(title=name[:256], description=(description or 'No description provided.')[:4000], color=discord.Color.blurple())
    embed.add_field(name='Creator', value=creator[:1024], inline=True)
    embed.add_field(name='Added by', value=interaction.user.mention, inline=True)
    if link:
        embed.add_field(name='Resource link', value=link[:1024], inline=False)
    uploaded = None
    if file:
        try:
            uploaded = await file.to_file()
        except discord.HTTPException:
            await interaction.response.send_message('I could not retrieve that attachment. Try a smaller file or use a link.', ephemeral=True)
            return
    try:
        posted = await channel.send(content=f'**New resource** · added by {interaction.user.mention}', embed=embed, file=uploaded)
    except discord.Forbidden:
        if uploaded: uploaded.close()
        await interaction.response.send_message('I cannot post there. Check View Channel, Send Messages, Embed Links, and Attach Files permissions.', ephemeral=True)
        return
    except discord.HTTPException:
        if uploaded: uploaded.close()
        await interaction.response.send_message('Discord could not post this resource. Check the file size and try again.', ephemeral=True)
        return
    await interaction.response.send_message(f'Posted in {channel.mention}: [open resource]({posted.jump_url})', ephemeral=True)

@bot.tree.command(name='search', description='Search resource posts in a channel.')
@app_commands.describe(query='Words from the resource name, creator, or description', channel='Channel to search; defaults to this channel')
async def search(interaction: discord.Interaction, query: str, channel: discord.TextChannel = None):
    if not await role_check(interaction): return
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message('Choose a text channel.', ephemeral=True); return
    await interaction.response.defer(ephemeral=True, thinking=True)
    found, needle = [], query.casefold()
    try:
        async for message in target.history(limit=500):
            for embed in message.embeds:
                creator = ' '.join(f.value for f in embed.fields if f.name.casefold() == 'creator')
                text = f'{embed.title or ""} {embed.description or ""} {creator}'.casefold()
                if needle in text:
                    found.append(message); break
            if len(found) >= 8: break
    except discord.Forbidden:
        await interaction.followup.send('I cannot read that channel history. Enable View Channel and Read Message History.', ephemeral=True); return
    if not found:
        await interaction.followup.send(f'No matches found in {target.mention} (checked up to 500 recent messages).', ephemeral=True); return
    lines = [f'• **{discord.utils.escape_markdown(m.embeds[0].title or "Untitled resource")}** — [open post]({m.jump_url})' for m in found]
    result = discord.Embed(title=f'Search results: {query[:180]}', description='\n'.join(lines), color=discord.Color.blurple())
    result.set_footer(text=f'Channel: #{target.name} · up to 8 results')
    await interaction.followup.send(embed=result, ephemeral=True)

@bot.tree.command(name='recent', description='Show recently added resources in a channel.')
@app_commands.describe(channel='Channel to check; defaults to this channel')
async def recent(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not await role_check(interaction): return
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message('Choose a text channel.', ephemeral=True); return
    await interaction.response.defer(ephemeral=True, thinking=True)
    lines = []
    try:
        async for message in target.history(limit=100):
            if message.embeds and message.embeds[0].title:
                lines.append(f'• **{discord.utils.escape_markdown(message.embeds[0].title)}** — [open post]({message.jump_url})')
                if len(lines) >= 8: break
    except discord.Forbidden:
        await interaction.followup.send('I cannot read that channel history. Enable View Channel and Read Message History.', ephemeral=True); return
    if not lines:
        await interaction.followup.send(f'No resource posts found in {target.mention}.', ephemeral=True); return
    embed = discord.Embed(title=f'Recently added · #{target.name}', description='\n'.join(lines), color=discord.Color.blurple())
    await interaction.followup.send(embed=embed, ephemeral=True)

bot.run(TOKEN)
