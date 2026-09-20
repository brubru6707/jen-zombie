"""
Canned worlds for Stage 3. The model is NOT wired here on purpose -- Stage 4
replaces pick_world() with the real image -> caption -> Nemotron pipeline.
Shape matches what the client expects and what the pipeline already emits.
"""
import random

BIOMES = ["lab", "rocky", "cave", "desert", "forest", "meadow", "snow", "water", "street", "farm"]

# Accent colours are per-biome so a swap is unmistakable on screen.
ACCENT = {
    "lab": "#4fd6ff", "rocky": "#9a8c7a", "cave": "#6d5f8c", "desert": "#e0b050",
    "forest": "#3fa35a", "meadow": "#7bd44f", "snow": "#dfe9f2", "water": "#3fa0d8",
    "street": "#8a8f98", "farm": "#c9a227",
}

CANNED = [
    {"name": "Server Room Shuffle", "biome": "lab", "count_scale": 1.0, "speed_scale": 0.8,
     "detect_scale": 1.3, "brute_bias": 0.1, "hp_bonus": 1,
     "barks": {"walker": ["Mind the cabling", "Static in my joints"],
               "runner": ["Fast as a fan spin"], "brute": ["I short the mains"],
               "calm": ["Careful, that rack is live.", "I only came for coffee."]}},
    {"name": "Quarry Descent", "biome": "rocky", "count_scale": 1.1, "speed_scale": 0.9,
     "detect_scale": 1.0, "brute_bias": 0.3, "hp_bonus": 2,
     "barks": {"walker": ["Gravel in my teeth"], "runner": ["Rockslide coming"],
               "brute": ["Boulders break easier"],
               "calm": ["Watch your footing here.", "Blasting finished hours ago."]}},
    {"name": "Dune Crawl", "biome": "desert", "count_scale": 1.3, "speed_scale": 1.0,
     "detect_scale": 1.5, "brute_bias": 0.15, "hp_bonus": 1,
     "barks": {"walker": ["Sand in everything"], "runner": ["No shade out here"],
               "brute": ["The heat feeds me"],
               "calm": ["Water's two ridges east.", "Sun'll cook you by noon."]}},
    {"name": "Tidal Shallows", "biome": "water", "count_scale": 0.9, "speed_scale": 0.7,
     "detect_scale": 1.1, "brute_bias": 0.2, "hp_bonus": 2,
     "barks": {"walker": ["Salt in the lungs"], "runner": ["The tide is faster"],
               "brute": ["I came in on the surf"],
               "calm": ["Tide turns in an hour.", "Nets came up empty again."]}},
    {"name": "Snowline Patrol", "biome": "snow", "count_scale": 1.0, "speed_scale": 0.75,
     "detect_scale": 0.9, "brute_bias": 0.25, "hp_bonus": 2,
     "barks": {"walker": ["Frost got my fingers"], "runner": ["Downhill is easy"],
               "brute": ["I am the avalanche"],
               "calm": ["Storm coming off the ridge.", "Keep your hands covered."]}},
    {"name": "Understory Ambush", "biome": "forest", "count_scale": 1.4, "speed_scale": 1.1,
     "detect_scale": 0.8, "brute_bias": 0.1, "hp_bonus": 0,
     "barks": {"walker": ["Branches hide us"], "runner": ["Through the undergrowth"],
               "brute": ["Trees fall for me"],
               "calm": ["Stay on the path.", "Something moved in the ferns."]}},
    {"name": "Kerbside Rush", "biome": "street", "count_scale": 1.5, "speed_scale": 1.2,
     "detect_scale": 1.2, "brute_bias": 0.2, "hp_bonus": 1,
     "barks": {"walker": ["Cross at the lights"], "runner": ["Traffic never stops"],
               "brute": ["I total cars"],
               "calm": ["Lights haven't worked for weeks.", "Nobody drives here now."]}},
    {"name": "Silo Shadow", "biome": "farm", "count_scale": 1.2, "speed_scale": 0.85,
     "detect_scale": 1.0, "brute_bias": 0.25, "hp_bonus": 1,
     "barks": {"walker": ["Harvest is late"], "runner": ["Through the rows"],
               "brute": ["I pull the plough"],
               "calm": ["Harvest went bad this year.", "Dog ran off that way."]}},
]

_order = []


def pick_world(seq=None):
    """Cycle through the canned set without repeating until exhausted."""
    global _order
    if not _order:
        _order = list(range(len(CANNED)))
        random.shuffle(_order)
    w = dict(CANNED[_order.pop()])
    w["accent"] = ACCENT.get(w["biome"], "#8a8f98")
    w["source"] = "canned"
    if seq is not None:
        w["seq"] = seq
    return w
