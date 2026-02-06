import os
import asyncio
import discord
import aiohttp
import asyncpg

from discord.ext import commands, tasks
from discord.ui import View, Modal, TextInput, Select
from urllib.parse import quote
from aiohttp import web

# ------------------ CONFIG ------------------

TOKEN = os.getenv("TOKEN")
RIOT_API_KEY = os.getenv("RIOT_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# ------------------ CONSTANTS ------------------
# EJEMPLO (ajusta a tu servidor)
REGIONS = {
    "EUW": ("euw1", "europe", 1409214841973112922),
    "LAN": ("la1", "americas", 1409214864064249990),
    "LAS": ("la2", "americas", 1415758108428341379),
}

SOLO_ROLES = {
    "UNRANKED": 1409215402999021753,
    "IRON": 1409215359952748565,
    "BRONZE": 1409215350373093570,
    "SILVER": 1409215342131286078,
    "GOLD": 1409215329758089226,
    "PLATINUM": 1409215319247163395,
    "EMERALD": 1409215310388662492,
    "DIAMOND": 1409215300452483194,
    "MASTER": 1409215289618333748,
    "GRANDMASTER": 1409215275601104996,
    "CHALLENGER": 1409214980653449307,
}

FLEX_ROLES = {
    "UNRANKED": 1468523211057528842,
    "IRON": 1468522994912727182,
    "BRONZE": 1468523378225840243,
    "SILVER": 1468523546975142004,
    "GOLD": 1468523603644514439,
    "PLATINUM": 1468523665040867401,
    "EMERALD": 1468523732791459991,
    "DIAMOND": 1468523804400554096,
    "MASTER": 1468523868913406017,
    "GRANDMASTER": 1468523924731199573,
    "CHALLENGER": 1468523984227405835,
}

PANEL_CHANNEL_ID = 1468511949368197191
LOG_CHANNEL_ID = 1410499822334640156

# ------------------ BOT ------------------

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------ DB ------------------

async def init_db():
    return await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5
    )

async def get_accounts(user_id):
    return await bot.db.fetch(
        "SELECT * FROM accounts WHERE user_id=$1 ORDER BY is_primary DESC",
        str(user_id)
    )

# ------------------ RIOT API ------------------

async def riot_get(url):
    headers = {"X-Riot-Token": RIOT_API_KEY}
    async with bot.http.get(url, headers=headers) as r:
        if r.status != 200:
            print("Riot API error:", r.status, url)
            return None
        return await r.json()

async def validate_riot_id(name, tag, region):
    _, routing, _ = REGIONS[region]
    return await riot_get(
        f"https://{routing}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
        f"{quote(name)}/{quote(tag)}"
    )

async def get_ranks(puuid, region):
    platform, _, _ = REGIONS[region]
    data = await riot_get(
        f"https://{platform}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
    )

    solo = flex = "UNRANKED"
    if data:
        for q in data:
            if q["queueType"] == "RANKED_SOLO_5x5":
                solo = q["tier"]
            elif q["queueType"] == "RANKED_FLEX_SR":
                flex = q["tier"]

    return solo, flex

# ------------------ ROLES ------------------

async def clear_roles(member):
    ids = list(SOLO_ROLES.values()) + list(FLEX_ROLES.values()) + [r[2] for r in REGIONS.values()]
    for rid in ids:
        role = member.guild.get_role(rid)
        if role and role in member.roles:
            await member.remove_roles(role)

async def apply_roles(member, region, solo, flex):
    await clear_roles(member)
    await member.add_roles(
        member.guild.get_role(REGIONS[region][2]),
        member.guild.get_role(SOLO_ROLES[solo]),
        member.guild.get_role(FLEX_ROLES[flex])
    )

# ------------------ LOGS ------------------

async def log_change(guild, member, riot_id, changes, source):
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if not channel:
        return

    embed = discord.Embed(title="Cambio de rango", color=0xED4245)
    embed.add_field(name="Usuario", value=member.mention, inline=False)
    embed.add_field(name="Cuenta", value=riot_id, inline=False)

    for c in changes:
        embed.add_field(
            name=c["type"],
            value=f"{c['before']} → {c['after']}",
            inline=False
        )

    embed.add_field(name="Origen", value=source, inline=False)
    await channel.send(embed=embed)

# ------------------ UI ------------------

class RegionDropdown(Select):
    def __init__(self, name, tag):
        self.name = name
        self.tag = tag
        super().__init__(
            placeholder="Selecciona región",
            options=[discord.SelectOption(label=r, value=r) for r in REGIONS]
        )

    async def callback(self, interaction):
        region = self.values[0]
        acc = await validate_riot_id(self.name, self.tag, region)

        if not acc:
            await interaction.response.send_message(
                "❌ Riot ID inválido.",
                ephemeral=True
            )
            return

        solo, flex = await get_ranks(acc["puuid"], region)

        async with bot.db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE accounts SET is_primary=false WHERE user_id=$1",
                    str(interaction.user.id)
                )
                await conn.execute(
                    """
                    INSERT INTO accounts (user_id, riot_id, puuid, region, solo, flex, is_primary)
                    VALUES ($1,$2,$3,$4,$5,$6,true)
                    """,
                    str(interaction.user.id),
                    f"{self.name}#{self.tag}",
                    acc["puuid"],
                    region,
                    solo,
                    flex
                )

        await apply_roles(interaction.user, region, solo, flex)
        await interaction.response.send_message(
            f"✅ **{self.name}#{self.tag}** vinculada",
            ephemeral=True
        )

class RegionView(View):
    def __init__(self, name, tag):
        super().__init__(timeout=60)
        self.add_item(RegionDropdown(name, tag))

class LinkModal(Modal, title="Vincular cuenta LoL"):
    riot = TextInput(label="Riot ID (Nombre#TAG)")

    async def on_submit(self, interaction):
        try:
            name, tag = self.riot.value.split("#")
        except ValueError:
            await interaction.response.send_message(
                "❌ Formato incorrecto.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Selecciona región:",
            view=RegionView(name, tag),
            ephemeral=True
        )

# ------------------ PANEL ------------------

class Panel(View):
    @discord.ui.button(label="Vincular cuenta", style=discord.ButtonStyle.primary)
    async def link(self, interaction, _):
        await interaction.response.send_modal(LinkModal())

# ------------------ AUTO REFRESH ------------------

@tasks.loop(hours=3)
async def auto_refresh():
    rows = await bot.db.fetch(
        "SELECT * FROM accounts WHERE is_primary=true"
    )

    for acc in rows:
        solo, flex = await get_ranks(acc["puuid"], acc["region"])
        await asyncio.sleep(1.2)

        if solo != acc["solo"] or flex != acc["flex"]:
            await bot.db.execute(
                """
                UPDATE accounts
                SET solo=$1, flex=$2
                WHERE user_id=$3 AND puuid=$4
                """,
                solo, flex, acc["user_id"], acc["puuid"]
            )

# ------------------ HEALTH ------------------

async def health(request):
    return web.Response(text="ok")

app = web.Application()
app.router.add_get("/", health)

# ------------------ READY ------------------

@bot.event
async def on_ready():
    bot.http = aiohttp.ClientSession()
    bot.db = await init_db()
    auto_refresh.start()

    channel = bot.get_channel(PANEL_CHANNEL_ID)
    if channel:
        await channel.send(
            embed=discord.Embed(
                title="🎮 Vinculación LoL",
                description="Gestiona tus cuentas",
                color=0x9146FF
            ),
            view=Panel()
        )

    print("Bot listo (Railway)")

# ------------------ RUN ------------------

if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    import asyncio
    from aiohttp import web

    async def start_bot_and_web():
        try:
            # Inicia el bot en background
            bot_task = asyncio.create_task(bot.start(TOKEN))

            # Inicia el servidor web (health endpoint)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080)))
            await site.start()

            print("✅ Bot y health endpoint corriendo")

            # Mantiene el bot vivo
            await bot_task

        except Exception as e:
            print("❌ Error al iniciar bot:", e)
            raise  # Permite que Railway vea que hubo error

    # Ejecuta todo en un solo loop
    asyncio.run(start_bot_and_web())
asyncio.run(start_bot_and_web())

