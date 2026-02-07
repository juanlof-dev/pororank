import discord, aiohttp, os
from discord.ext import commands, tasks
from discord.ui import View, Modal, TextInput, Select
from urllib.parse import quote
from flask import Flask
import threading

from config import *
from database import load_data, save_data, init_db

# ------------------ BOT ------------------

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------ VERIFICACIÓN ------------------

PENDING_VERIFICATIONS = {}
VERIFICATION_ICON_ID = 29  # cambia este ID si quieres otro icono

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
    return await riot_get(
        f"https://{routing}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{quote(name)}/{quote(tag)}"
    )

async def get_summoner_by_puuid(puuid, region):
    platform, _, _ = REGIONS[region]
    return await riot_get(
        f"https://{platform}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
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

# ------------------ EMBEDS ------------------

def verification_embed(name, tag):
    embed = discord.Embed(
        title="🔐 Verificación de propiedad",
        description=(
            f"Para verificar que eres el dueño de **{name}#{tag}**:\n\n"
            "1️⃣ Abre el cliente de **League of Legends**\n"
            "2️⃣ Cambia tu **icono de invocador** por el siguiente\n\n"
            "Cuando lo hayas hecho, pulsa **He cambiado el icono**"
        ),
        color=0xF1C40F
    )
    embed.set_thumbnail(
        url=f"https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/profile-icons/{VERIFICATION_ICON_ID}.jpg"
    )
    return embed

def build_account_embed(riot_id, summoner, region, solo, flex):
    icon_url = (
        f"https://raw.communitydragon.org/latest/plugins/"
        f"rcp-be-lol-game-data/global/default/v1/profile-icons/{summoner['profileIconId']}.jpg"
    )

    embed = discord.Embed(title="🎮 Cuenta de League of Legends", color=0x2ECC71)
    embed.set_thumbnail(url=icon_url)

    embed.add_field(name="Nombre de invocador", value=f"{riot_id} ({region})", inline=True)
    embed.add_field(name="Nivel", value=summoner["summonerLevel"], inline=True)
    embed.add_field(name="Clasificación Solo/Duo", value=solo, inline=False)
    embed.add_field(name="Clasificación Flexible", value=flex, inline=False)

    return embed

# ------------------ VERIFICACIÓN VIEW ------------------

class VerifyIconView(View):
    def __init__(self, user_id):
        super().__init__(timeout=300)
        self.user_id = user_id

    @discord.ui.button(label="He cambiado el icono", style=discord.ButtonStyle.success)
    async def verify(self, interaction, _):
        uid = str(interaction.user.id)

        if uid != self.user_id:
            await interaction.response.send_message("❌ Esta verificación no es tuya.", ephemeral=True)
            return

        pending = PENDING_VERIFICATIONS.get(uid)
        if not pending:
            await interaction.response.send_message("⏰ Verificación expirada.", ephemeral=True)
            return

        summoner = await get_summoner_by_puuid(pending["puuid"], pending["region"])
        if summoner["profileIconId"] != VERIFICATION_ICON_ID:
            await interaction.response.send_message(
                "❌ El icono no coincide. Cámbialo y vuelve a intentarlo.",
                ephemeral=True
            )
            return

        solo, flex = await get_ranks(pending["puuid"], pending["region"])
        data = load_data()
        data.setdefault(uid, [])

        for a in data[uid]:
            a["primary"] = False

        data[uid].append({
            "riot_id": pending["riot_id"],
            "puuid": pending["puuid"],
            "region": pending["region"],
            "solo": solo,
            "flex": flex,
            "primary": True
        })

        save_data(data)
        await apply_roles(interaction.user, pending["region"], solo, flex)

        embed = build_account_embed(
            pending["riot_id"],
            summoner,
            pending["region"],
            solo,
            flex
        )

        del PENDING_VERIFICATIONS[uid]

        await interaction.response.send_message(
            "✅ **Cuenta vinculada correctamente**",
            embed=embed,
            ephemeral=True
        )

# ------------------ LINK FLOW ------------------

class RegionDropdown(Select):
    def __init__(self, name, tag):
        self.name = name
        self.tag = tag
        options = [discord.SelectOption(label=r, value=r) for r in REGIONS.keys()]
        super().__init__(placeholder="Selecciona región", options=options)

    async def callback(self, interaction):
        region = self.values[0]
        acc = await validate_riot_id(self.name, self.tag, region)

        if not acc:
            await interaction.response.send_message("❌ Riot ID no válido.", ephemeral=True)
            return

        PENDING_VERIFICATIONS[str(interaction.user.id)] = {
            "riot_id": f"{self.name}#{self.tag}",
            "puuid": acc["puuid"],
            "region": region
        }

        await interaction.response.send_message(
            embed=verification_embed(self.name, self.tag),
            view=VerifyIconView(str(interaction.user.id)),
            ephemeral=True
        )

class RegionView(View):
    def __init__(self, name, tag):
        super().__init__()
        self.add_item(RegionDropdown(name, tag))

class LinkModal(Modal, title="Vincular cuenta LoL"):
    riot = TextInput(label="Riot ID (Nombre#TAG)")

    async def on_submit(self, interaction):
        try:
            name, tag = self.riot.value.strip().split("#")
        except ValueError:
            await interaction.response.send_message("❌ Formato incorrecto.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Selecciona la región:",
            view=RegionView(name, tag),
            ephemeral=True
        )

# ------------------ PANEL ------------------

class Panel(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Vincular cuenta", style=discord.ButtonStyle.primary)
    async def link(self, interaction, _):
        await interaction.response.send_modal(LinkModal())

    @discord.ui.button(label="Ver cuentas", style=discord.ButtonStyle.secondary)
    async def view_accounts(self, interaction, _):
        data = load_data().get(str(interaction.user.id), [])
        if not data:
            await interaction.response.send_message("No tienes cuentas vinculadas.", ephemeral=True)
            return

        for acc in data:
            summoner = await get_summoner_by_puuid(acc["puuid"], acc["region"])
            embed = build_account_embed(
                acc["riot_id"],
                summoner,
                acc["region"],
                acc["solo"],
                acc["flex"]
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

# ------------------ DEPLOY PANEL ------------------

async def deploy_panel():
    channel = bot.get_channel(PANEL_CHANNEL_ID)
    await channel.purge(limit=5)

    embed = discord.Embed(
        title="🎮 Vinculación de Cuentas LoL",
        description="Gestiona tus cuentas de League of Legends desde aquí.",
        color=0x9146FF
    )

    await channel.send(embed=embed, view=Panel())

# ------------------ READY ------------------

@bot.event
async def on_ready():
    init_db()
    bot.add_view(Panel())
    await deploy_panel()
    print("Bot listo")

# ------------------ WEB SERVER ------------------

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot activo", 200

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))

threading.Thread(target=run_flask).start()

# ------------------ START ------------------

bot.run(TOKEN)
