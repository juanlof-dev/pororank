import discord, aiohttp, os
from discord.ext import commands, tasks
from discord.ui import View, Modal, TextInput, Select
from urllib.parse import quote

from config import *
from database import load_data, save_data, init_db

# ------------------ BOT ------------------

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------ RIOT API ------------------

async def riot_get(url):
    headers = {"X-Riot-Token": RIOT_API_KEY}
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers) as r:
            if r.status != 200:
                print("Riot API error:", r.status, url)
                return None
            return await r.json()

async def validate_riot_id(name, tag, region):
    _, routing, _ = REGIONS[region]
    name = quote(name)
    tag = quote(tag)
    return await riot_get(
        f"https://{routing}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{name}/{tag}"
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
        embed.add_field(name=c["type"], value=f"{c['before']} → {c['after']}", inline=False)

    embed.add_field(name="Origen", value=source, inline=False)
    await channel.send(embed=embed)

# ------------------ LINK FLOW ------------------

class RegionDropdown(Select):
    def __init__(self, name, tag):
        self.name = name
        self.tag = tag
        options = [discord.SelectOption(label=r, value=r) for r in REGIONS.keys()]
        super().__init__(placeholder="Selecciona región", options=options, custom_id=f"region_dropdown_{name}_{tag}")

    async def callback(self, interaction):
        region = self.values[0]
        acc = await validate_riot_id(self.name, self.tag, region)
        if not acc:
            await interaction.response.send_message("❌ Riot ID no válido o API inaccesible.", ephemeral=True)
            return

        solo, flex = await get_ranks(acc["puuid"], region)
        data = load_data()
        uid = str(interaction.user.id)
        data.setdefault(uid, [])

        for a in data[uid]:
            a["primary"] = False

        data[uid].append({
            "riot_id": f"{self.name}#{self.tag}",
            "puuid": acc["puuid"],
            "region": region,
            "solo": solo,
            "flex": flex,
            "primary": True
        })

        save_data(data)
        await apply_roles(interaction.user, region, solo, flex)

        await interaction.response.send_message(
            f"✅ **{self.name}#{self.tag}** vinculada\nSoloQ: {solo}\nFlexQ: {flex}", ephemeral=True
        )

class RegionView(View):
    def __init__(self, name, tag):
        super().__init__(timeout=None)
        self.add_item(RegionDropdown(name, tag))

class LinkModal(Modal, title="Vincular cuenta LoL"):
    warning = TextInput(
        label="Aviso",
        default="⚠️ Nunca compartas contraseñas ni información confidencial.",
        required=False,
        style=discord.TextStyle.paragraph
    )
    riot = TextInput(label="Riot ID (Nombre#TAG)")

    async def on_submit(self, interaction):
        try:
            name, tag = self.riot.value.strip().split("#")
        except ValueError:
            await interaction.response.send_message("❌ Formato incorrecto. Usa Nombre#TAG", ephemeral=True)
            return

        await interaction.response.send_message("Selecciona la región:", view=RegionView(name, tag), ephemeral=True)

# ------------------ VIEW / DELETE ACCOUNTS ------------------

class DeleteAccountSelect(Select):
    def __init__(self, user_id):
        self.user_id = user_id
        data = load_data().get(user_id, [])
        options = [
            discord.SelectOption(label=f"{'⭐ ' if a['primary'] else ''}{a['riot_id']} ({a['region']})", value=str(i))
            for i, a in enumerate(data)
        ]
        super().__init__(placeholder="Eliminar cuenta", options=options, custom_id=f"delete_account_{user_id}")

    async def callback(self, interaction):
        idx = int(self.values[0])
        data = load_data()
        accs = data.get(self.user_id, [])
        removed = accs.pop(idx)

        if accs:
            accs[0]["primary"] = True
            await apply_roles(interaction.user, accs[0]["region"], accs[0]["solo"], accs[0]["flex"])
        else:
            await clear_roles(interaction.user)

        save_data(data)
        await interaction.response.send_message(f"🗑️ Cuenta **{removed['riot_id']}** eliminada", ephemeral=True)

class AccountsView(View):
    def __init__(self, user_id):
        super().__init__(timeout=None)
        self.add_item(DeleteAccountSelect(user_id))

# ------------------ PANEL ------------------

class Panel(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Vincular cuenta", style=discord.ButtonStyle.primary, custom_id="panel_link")
    async def link(self, interaction, _):
        await interaction.response.send_modal(LinkModal())

    @discord.ui.button(label="Ver cuentas", style=discord.ButtonStyle.secondary, custom_id="panel_view_accounts")
    async def view_accounts(self, interaction, _):
        data = load_data().get(str(interaction.user.id), [])
        if not data:
            await interaction.response.send_message("No tienes cuentas vinculadas.", ephemeral=True)
            return

        msg = "\n".join(
            f"{'⭐' if a['primary'] else ''} {a['riot_id']} | {a['region']} | SoloQ: {a['solo']} | FlexQ: {a['flex']}"
            for a in data
        )
        await interaction.response.send_message(msg, view=AccountsView(str(interaction.user.id)), ephemeral=True)

    @discord.ui.button(label="Actualizar datos", style=discord.ButtonStyle.success, custom_id="panel_refresh")
    async def refresh(self, interaction, _):
        data = load_data()
        uid = str(interaction.user.id)
        if uid not in data:
            await interaction.response.send_message("No tienes cuenta primaria.", ephemeral=True)
            return

        primary = next(a for a in data[uid] if a["primary"])
        solo, flex = await get_ranks(primary["puuid"], primary["region"])
        await apply_roles(interaction.user, primary["region"], solo, flex)
        await interaction.response.send_message("🔄 Datos actualizados correctamente.", ephemeral=True)

# ------------------ DEPLOY PANEL ------------------

async def deploy_panel():
    channel = bot.get_channel(PANEL_CHANNEL_ID)
    if not channel:
        return

    await channel.purge(limit=5)

    embed = discord.Embed(
        title="🎮 Vinculación de Cuentas LoL",
        description=(
            "Gestiona tus cuentas de **League of Legends**, roles y rangos directamente desde este panel.\n\n"
            "🔹 **Vincular cuenta:** Añade tu cuenta de LoL\n"
            "🔹 **Ver cuentas:** Consulta tus cuentas vinculadas\n"
            "🔹 **Actualizar datos:** Refresca tu rango automáticamente"
        ),
        color=0x9146FF
    )

    embed.set_thumbnail(url="https://upload.wikimedia.org/wikipedia/en/7/77/League_of_Legends_Logo.png")
    embed.set_footer(text="Panel oficial de vinculación | ¡Mantén tus roles actualizados!", icon_url=bot.user.display_avatar.url)

    await channel.send(embed=embed, view=Panel())

# ------------------ AUTO REFRESH ------------------

@tasks.loop(hours=3)
async def auto_refresh():
    data = load_data()
    for guild in bot.guilds:
        for member in guild.members:
            uid = str(member.id)
            if uid not in data:
                continue

            primary = next(a for a in data[uid] if a["primary"])
            solo, flex = await get_ranks(primary["puuid"], primary["region"])

            changes = []
            if solo != primary["solo"]:
                changes.append({"type": "SoloQ", "before": primary["solo"], "after": solo})
                primary["solo"] = solo
            if flex != primary["flex"]:
                changes.append({"type": "FlexQ", "before": primary["flex"], "after": flex})
                primary["flex"] = flex

            if changes:
                await apply_roles(member, primary["region"], solo, flex)
                await log_change(guild, member, primary["riot_id"], changes, "AUTO_REFRESH")

    save_data(data)

# ------------------ READY ------------------

@bot.event
async def on_ready():
    init_db()
    # registrar views persistentes
    bot.add_view(Panel())
    await deploy_panel()
    auto_refresh.start()
    print("Bot listo")

# ------------------ SERVIDOR WEB PARA UPTIMEROBOT ------------------
from flask import Flask
import threading

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot activo ✅", 200

def run_flask():
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask).start()

# ------------------ INICIAR BOT ------------------
bot.run(TOKEN)
