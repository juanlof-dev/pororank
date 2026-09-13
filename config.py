import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TOKEN")
RIOT_API_KEY = os.getenv("RIOT_API_KEY")

HYBRID_MODE = True  # 👈 IMPORTANTE

# 📌 Canales
PANEL_CHANNEL_ID = 1468511949368197191
LOG_CHANNEL_ID = 1410499822334640156


REGIONS = {
    "EUW": ("euw1", "europe", 1409214841973112922),
    "EUNE": ("eun1", "europe", 1002),
    "NA": ("na1", "americas", 1003),
    "KR": ("kr", "asia", 1004),
    "BR": ("br1", "americas", 1005),
    "LAN": ("la1", "americas", 1409214864064249990),
    "LAS": ("la2", "americas", 1415758108428341379),
    "OCE": ("oc1", "americas", 1008),
    "JP": ("jp1", "asia", 1009),
    "TR": ("tr1", "europe", 1010),
    "RU": ("ru", "europe", 1011),
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

# 📌 Canales de "Buscar partida" — uno por tier de SoloQ
TIER_CHANNELS = {
    "UNRANKED": 1547299799550656592,     # #sin-clasificar
    "IRON": 1547299871361470484,          # #hierro
    "BRONZE": 1547299934678679644,        # #bronce
    "SILVER": 1547299999883202662,        # #plata
    "GOLD": 1547300037443326062,          # #oro
    "PLATINUM": 1547300099812499516,      # #platino
    "EMERALD": 1547300192594821131,       # #esmeralda
    "DIAMOND": 1547300248265957427,       # #diamante
    "MASTER": 1547300300862390382,        # #maestro
    "GRANDMASTER": 1547300350975811614,   # #granmaestro
    "CHALLENGER": 1547300414792147006,    # #aspirante
}

# 📌 A qué canales de tier se publica el aviso según el tier de SoloQ del
# jugador. Es una tabla explícita (no una fórmula) porque los extremos son
# asimétricos a propósito: Sinrango no se propaga a Hierro, y Challenger no
# tiene tier superior.
TIER_SEARCH_WINDOWS = {
    "UNRANKED": ["UNRANKED"],
    "IRON": ["IRON", "BRONZE"],
    "BRONZE": ["IRON", "BRONZE", "SILVER"],
    "SILVER": ["BRONZE", "SILVER", "GOLD"],
    "GOLD": ["SILVER", "GOLD", "PLATINUM"],
    "PLATINUM": ["GOLD", "PLATINUM", "EMERALD"],
    "EMERALD": ["PLATINUM", "EMERALD", "DIAMOND"],
    "DIAMOND": ["EMERALD", "DIAMOND", "MASTER"],
    "MASTER": ["DIAMOND", "MASTER", "GRANDMASTER"],
    "GRANDMASTER": ["MASTER", "GRANDMASTER", "CHALLENGER"],
    "CHALLENGER": ["GRANDMASTER", "CHALLENGER"],
}

# 📌 Roles de posición/lane asignados por las preguntas de incorporación
# (onboarding) del propio Discord — el bot solo los LEE, no los asigna.
LANE_ROLES = {
    "TOP": 1548391864808251628,      # Toplane
    "JUNGLE": 1548392101266456646,   # Jungla
    "MID": 1548392220321906770,      # Midlane
    "ADC": 1548392326177619979,      # Botlane
    "SUPPORT": 1548392411535908926,  # Soporte
}

# 📌 Cooldown del botón "Buscar partida", en segundos
SEARCH_COOLDOWN_SECONDS = 600  # 10 minutos
