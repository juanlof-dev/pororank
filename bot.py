import discord
import aiohttp
import os
import threading
import asyncio
from urllib.parse import quote
from discord.ext import commands, tasks
from discord.ui import View, Modal, TextInput, Select
from flask import Flask

from config import *
from database import load_data, save_data, init_db


intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

PENDING_VERIFICATIONS = {}
VERIFICATION_ICON_ID = 25

SIN_VINCULAR_ROLE_ID = 1547926475657969724


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
        f"https://{routing}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
        f"{quote(name)}/{quote(tag)}"
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


def get_desired_roles(member, region, solo, flex):
    roles = []

    region_role = member.guild.get_role(REGIONS[region][2])
    solo_role = member.guild.get_role(SOLO_ROLES[solo])
    flex_role = member.guild.get_role(FLEX_ROLES[flex])

    for r in (region_role, solo_role, flex_role):
        if r:
            roles.append(r)

    return set(roles)


async def apply_roles(member, region, solo, flex):
    desired = get_desired_roles(member, region, solo, flex)

    managed_ids = (
        list(SOLO_ROLES.values())
        + list(FLEX_ROLES.values())
        + [r[2] for r in REGIONS.values()]
    )

    current = {
        r for r in member.roles
        if r.id in managed_ids
    }

    to_add = desired - current
    to_remove = current - desired

    # CAMBIO 1:
    # Si el usuario tiene una cuenta vinculada,
    # se elimina el rol "Sin vincular".
    sin_vincular = member.guild.get_role(SIN_VINCULAR_ROLE_ID)

    if sin_vincular and sin_vincular in member.roles:
        await member.remove_roles(sin_vincular)

    if not to_add and not to_remove:
        print(f"[ROLES] {member} sin cambios, no se tocarán roles")
        return

    print(
        f"[ROLES] {member} "
        f"+{[r.name for r in to_add]} "
        f"-{[r.name for r in to_remove]}"
    )

    if to_remove:
        await member.remove_roles(*to_remove)

    if to_add:
        await member.add_roles(*to_add)


async def clear_roles(member):
    managed_ids = (
        list(SOLO_ROLES.values())
        + list(FLEX_ROLES.values())
        + [r[2] for r in REGIONS.values()]
    )

    roles = [
        r for r in member.roles
        if r.id in managed_ids
    ]

    if roles:
        await member.remove_roles(*roles)


def verification_embed(member):
    embed = discord.Embed(
        title="Verificación",
        description=(
            f"{member.mention}, para verificar tu cuenta debes "
            "utilizar el icono de invocador indicado."
        ),
        color=discord.Color.blurple()
    )

    embed.set_footer(text="La Grieta ES")

    return embed


def build_account_embed(accounts):
    embed = discord.Embed(
        title="Tus cuentas vinculadas",
        color=discord.Color.blurple()
    )

    for i, account in enumerate(accounts):
        primary = " ⭐ PRINCIPAL" if i == 0 else ""

        embed.add_field(
            name=f"{account['name']}#{account['tag']}{primary}",
            value=(
                f"Región: {account['region']}\n"
                f"SoloQ: {account.get('solo', 'UNRANKED')}\n"
                f"FlexQ: {account.get('flex', 'UNRANKED')}"
            ),
            inline=False
        )

    return embed


class VerifyIconView(View):

    def __init__(self, riot_name, riot_tag, region, member):
        super().__init__(timeout=300)

        self.riot_name = riot_name
        self.riot_tag = riot_tag
        self.region = region
        self.member = member

    @discord.ui.button(
        label="Verificar",
        style=discord.ButtonStyle.success
    )
    async def verify(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        key = interaction.user.id

        if key not in PENDING_VERIFICATIONS:
            await interaction.response.send_message(
                "Esta verificación ya no está disponible.",
                ephemeral=True
            )
            return

        account = PENDING_VERIFICATIONS[key]

        summoner = await get_summoner_by_puuid(
            account["puuid"],
            account["region"]
        )

        if not summoner:
            await interaction.response.send_message(
                "No se ha podido obtener la información de tu cuenta.",
                ephemeral=True
            )
            return

        if summoner.get("profileIconId") != VERIFICATION_ICON_ID:
            await interaction.response.send_message(
                "El icono de invocador no coincide con el indicado.",
                ephemeral=True
            )
            return

        solo, flex = await get_ranks(
            account["puuid"],
            account["region"]
        )

        account["solo"] = solo
        account["flex"] = flex

        data = load_data()

        user_id = str(interaction.user.id)

        if user_id not in data:
            data[user_id] = []

        data[user_id].append(account)

        save_data(data)

        # Aplicamos los roles.
        # Aquí también se elimina "Sin vincular".
        await apply_roles(
            interaction.user,
            account["region"],
            solo,
            flex
        )

        del PENDING_VERIFICATIONS[key]

        await interaction.response.send_message(
            "✅ Cuenta vinculada correctamente.",
            ephemeral=True
        )


class AccountActionsView(View):

    def __init__(self, accounts):
        super().__init__(timeout=300)

        self.accounts = accounts

        options = []

        for i, account in enumerate(accounts):
            options.append(
                discord.SelectOption(
                    label=f"{account['name']}#{account['tag']}",
                    value=str(i)
                )
            )

        self.account_select = Select(
            placeholder="Selecciona una cuenta",
            options=options
        )

        self.account_select.callback = self.account_selected

        self.add_item(self.account_select)

    async def account_selected(self, interaction):
        index = int(self.account_select.values[0])

        account = self.accounts[index]

        embed = discord.Embed(
            title=f"{account['name']}#{account['tag']}",
            color=discord.Color.blurple()
        )

        embed.add_field(
            name="Región",
            value=account["region"]
        )

        embed.add_field(
            name="SoloQ",
            value=account.get("solo", "UNRANKED")
        )

        embed.add_field(
            name="FlexQ",
            value=account.get("flex", "UNRANKED")
        )

        view = AccountActionsButtons(
            self.accounts,
            index
        )

        await interaction.response.edit_message(
            embed=embed,
            view=view
        )


class AccountActionsButtons(View):

    def __init__(self, accounts, selected_index):
        super().__init__(timeout=300)

        self.accounts = accounts
        self.selected_index = selected_index

    @discord.ui.button(
        label="Hacer principal",
        style=discord.ButtonStyle.primary
    )
    async def primary(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        account = self.accounts.pop(self.selected_index)

        self.accounts.insert(0, account)

        data = load_data()

        data[str(interaction.user.id)] = self.accounts

        save_data(data)

        await apply_roles(
            interaction.user,
            account["region"],
            account.get("solo", "UNRANKED"),
            account.get("flex", "UNRANKED")
        )

        await interaction.response.send_message(
            "⭐ Cuenta marcada como principal.",
            ephemeral=True
        )

    @discord.ui.button(
        label="Eliminar",
        style=discord.ButtonStyle.danger
    )
    async def delete(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        self.accounts.pop(self.selected_index)

        data = load_data()

        user_id = str(interaction.user.id)

        if self.accounts:
            # La primera cuenta pasa a ser la principal.
            data[user_id] = self.accounts

            save_data(data)

            account = self.accounts[0]

            await apply_roles(
                interaction.user,
                account["region"],
                account.get("solo", "UNRANKED"),
                account.get("flex", "UNRANKED")
            )

        else:
            # No quedan cuentas vinculadas.
            data.pop(user_id, None)

            save_data(data)

            await clear_roles(interaction.user)

            # CAMBIO 2:
            # Si el usuario se queda sin ninguna cuenta vinculada,
            # se vuelve a añadir "Sin vincular".
            sin_vincular = interaction.user.guild.get_role(
                SIN_VINCULAR_ROLE_ID
            )

            if (
                sin_vincular
                and sin_vincular not in interaction.user.roles
            ):
                await interaction.user.add_roles(sin_vincular)

        await interaction.response.send_message(
            "Cuenta eliminada.",
            ephemeral=True
        )


class RegionDropdown(Select):

    def __init__(self):
        options = [
            discord.SelectOption(
                label=name,
                value=name
            )
            for name in REGIONS.keys()
        ]

        super().__init__(
            placeholder="Selecciona tu región",
            options=options
        )

    async def callback(self, interaction):
        region = self.values[0]

        modal = LinkModal(region)

        await interaction.response.send_modal(modal)


class RegionView(View):

    def __init__(self):
        super().__init__(timeout=300)

        self.add_item(RegionDropdown())


class LinkModal(Modal):

    def __init__(self, region):
        super().__init__(title="Vincular cuenta")

        self.region = region

        self.name = TextInput(
            label="Nombre de invocador",
            placeholder="Ejemplo: Faker",
            required=True
        )

        self.tag = TextInput(
            label="Tag",
            placeholder="Ejemplo: KR1",
            required=True
        )

        self.add_item(self.name)
        self.add_item(self.tag)

    async def on_submit(self, interaction):
        name = self.name.value.strip()
        tag = self.tag.value.strip()

        account = await validate_riot_id(
            name,
            tag,
            self.region
        )

        if not account:
            await interaction.response.send_message(
                "No se ha encontrado esa cuenta.",
                ephemeral=True
            )
            return

        puuid = account["puuid"]

        PENDING_VERIFICATIONS[interaction.user.id] = {
            "name": name,
            "tag": tag,
            "puuid": puuid,
            "region": self.region
        }

        embed = verification_embed(
            interaction.user
        )

        await interaction.response.send_message(
            embed=embed,
            view=VerifyIconView(
                name,
                tag,
                self.region,
                interaction.user
            ),
            ephemeral=True
        )


class Panel(View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Vincular cuenta",
        style=discord.ButtonStyle.success,
        custom_id="panel_link"
    )
    async def link(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        await interaction.response.send_message(
            "Selecciona tu región:",
            view=RegionView(),
            ephemeral=True
        )

    @discord.ui.button(
        label="Mis cuentas",
        style=discord.ButtonStyle.primary,
        custom_id="panel_accounts"
    )
    async def accounts(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        data = load_data()

        accounts = data.get(
            str(interaction.user.id),
            []
        )

        if not accounts:
            await interaction.response.send_message(
                "No tienes ninguna cuenta vinculada.",
                ephemeral=True
            )
            return

        embed = build_account_embed(accounts)

        await interaction.response.send_message(
            embed=embed,
            view=AccountActionsView(accounts),
            ephemeral=True
        )

    @discord.ui.button(
        label="Actualizar",
        style=discord.ButtonStyle.secondary,
        custom_id="panel_refresh"
    )
    async def refresh(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        data = load_data()

        accounts = data.get(
            str(interaction.user.id),
            []
        )

        if not accounts:
            await interaction.response.send_message(
                "No tienes ninguna cuenta vinculada.",
                ephemeral=True
            )
            return

        account = accounts[0]

        solo, flex = await get_ranks(
            account["puuid"],
            account["region"]
        )

        account["solo"] = solo
        account["flex"] = flex

        data[str(interaction.user.id)] = accounts

        save_data(data)

        await apply_roles(
            interaction.user,
            account["region"],
            solo,
            flex
        )

        await interaction.response.send_message(
            "🔄 Datos actualizados correctamente.",
            ephemeral=True
        )


@tasks.loop(hours=12)
async def update_ranks_loop():

    data = load_data()

    changed = False

    for user_id, accounts in data.items():

        if not accounts:
            continue

        try:
            guild = bot.guilds[0]

            member = guild.get_member(
                int(user_id)
            )

            if not member:
                continue

            account = accounts[0]

            solo, flex = await get_ranks(
                account["puuid"],
                account["region"]
            )

            if (
                account.get("solo") != solo
                or account.get("flex") != flex
            ):
                account["solo"] = solo
                account["flex"] = flex

                changed = True

            await apply_roles(
                member,
                account["region"],
                solo,
                flex
            )

        except Exception as e:
            print(
                f"[UPDATE] Error actualizando {user_id}:",
                e
            )

    if changed:
        save_data(data)


async def deploy_panel():

    for guild in bot.guilds:

        channel = guild.system_channel

        if not channel:
            continue

        try:
            messages = [
                message async for message in channel.history(
                    limit=5
                )
            ]

            for message in messages:
                if message.author == bot.user:
                    try:
                        await message.delete()
                    except:
                        pass

            embed = discord.Embed(
                title="La Grieta ES",
                description=(
                    "Vincula tu cuenta de League of Legends "
                    "para obtener automáticamente tus roles "
                    "de región y clasificación."
                ),
                color=discord.Color.blurple()
            )

            embed.set_footer(
                text="La Grieta ES"
            )

            await channel.send(
                embed=embed,
                view=Panel()
            )

        except Exception as e:
            print(
                "[PANEL] Error:",
                e
            )


@bot.event
async def on_ready():

    print(
        f"Conectado como {bot.user}"
    )

    init_db()

    # Eliminamos el comando /duo si existiera.
    try:
        bot.tree.remove_command(
            "duo"
        )

        await bot.tree.sync()

    except Exception as e:
        print(
            "[COMMAND] Error:",
            e
        )

    # Vistas persistentes.
    bot.add_view(
        Panel()
    )

    await deploy_panel()

    if not update_ranks_loop.is_running():
        update_ranks_loop.start()


app = Flask(__name__)


@app.route("/")
def home():
    return "La Grieta ES Bot OK"


def run_flask():
    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                8080
            )
        )
    )


if __name__ == "__main__":

    threading.Thread(
        target=run_flask,
        daemon=True
    ).start()

    bot.run(TOKEN)
