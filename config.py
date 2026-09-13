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
    "SIN RANGO": 1547293381753831435,
    "HIERRO": 1547293370643255418,
    "BRONCE": 1547293360488980502,
    "PLATA": 1547293349692571799,
    "ORO": 1547293338645045310,
    "PLATINO": 1547293325110022195,
    "ESMERALDA": 1547293313734807704,
    "DIAMANTE": 1547293302687137902,
    "MAESTRO": 1547293227038543963,
    "GRAN MAESTRO": 1547293205265911969,
    "ASPIRANTE": 1547293109224873995,
}

FLEX_ROLES = {
    "SIN RANGO": 1547297479496704010,
    "HIERRO": 1547297468943704154,
    "BRONCE": 1547297456658718882,
    "PLATA": 1547297448261591090,
    "ORO": 1547297439357083838,
    "PLATINO": 1547297430582726716,
    "ESMERALDA": 1547297421376229446,
    "DIAMANTE": 1547297412681568376,
    "MAESTRO": 1547297403403505735,
    "GRAN MAESTRO": 1547297393362337810,
    "ASPIRANTE": 1547297309065347102,
}

# 📌 Canales de "Buscar partida" — uno por tier de SoloQ
TIER_CHANNELS = {
    "SIN RANGO": 1547299799550656592,     # #sin-clasificar
    "HIERRO": 1547299871361470484,          # #hierro
    "BRONCE": 1547299934678679644,        # #bronce
    "PLATA": 1547299999883202662,        # #plata
    "ORO": 1547300037443326062,          # #oro
    "PLATINO": 1547300099812499516,      # #platino
    "ESMERALDA": 1547300192594821131,       # #esmeralda
    "DIAMANTE": 1547300248265957427,       # #diamante
    "MAESTRO": 1547300300862390382,        # #maestro
    "GRAN MAESTRO": 1547300350975811614,   # #granmaestro
    "ASPIRANTE": 1547300414792147006,    # #aspirante
}

# 📌 A qué canales de tier se publica el aviso según el tier de SoloQ del
# jugador. Es una tabla explícita (no una fórmula) porque los extremos son
# asimétricos a propósito: Sinrango no se propaga a Hierro, y Challenger no
# tiene tier superior.
TIER_SEARCH_WINDOWS = {
    "SIN RANGO": ["SIN RANGO"],
    "HIERRO": ["HIERRO", "BRONCE"],
    "BRONCE": ["HIERRO", "BRONCE", "PLATA"],
    "PLATA": ["BRONCE", "PLATA", "ORO"],
    "ORO": ["PLATA", "ORO", "PLATINO"],
    "PLATINO": ["ORO", "PLATINO", "ESMERALDA"],
    "ESMERALDA": ["PLATINO", "ESMERALDA", "DIAMANTE"],
    "DIAMANTE": ["ESMERALDA", "DIAMANTE", "MAESTRO"],
    "MAESTRO": ["DIAMANTE", "MAESTRO", "GRAN MAESTRO"],
    "GRAN MAESTRO": ["MAESTRO", "GRAN MAESTRO", "ASPIRANTE"],
    "ASPIRANTE": ["GRAN MAESTRO", "ASPIRANTE"],
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
