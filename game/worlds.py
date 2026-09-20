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
     "barks": {
               "walker": [
                   "Mind the cabling",
                   "Static in my joints",
                   "The racks remember me",
                   "Cold aisle, colder hands",
                   "I was on night shift",
                   "My badge still works"],
               "runner": [
                   "Fast as a fan spin",
                   "Uptime means nothing now",
                   "Through the cable trays",
                   "I clock faster than you",
                   "No latency in here",
                   "Catch me at the patch panel"],
               "brute": [
                   "I short the mains",
                   "Racks tip easy",
                   "I am the power surge",
                   "Steel doors bend inward",
                   "Your backup will not help",
                   "I break the cooling"],
               "calm": [
                   "Careful, that rack is live.",
                   "I only came for coffee.",
                   "The cooling failed again last night.",
                   "Badge reader has been broken for weeks.",
                   "Don't touch anything blinking amber.",
                   "I keep meaning to file a ticket."]}},
    {"name": "Quarry Descent", "biome": "rocky", "count_scale": 1.1, "speed_scale": 0.9,
     "detect_scale": 1.0, "brute_bias": 0.3, "hp_bonus": 2,
     "barks": {
               "walker": [
                   "Gravel in my teeth",
                   "I fell from the top",
                   "Dust settles on everything",
                   "The seam gave way",
                   "My boots are full of stone",
                   "Slow going on this slope"],
               "runner": [
                   "Rockslide coming",
                   "Downhill all the way",
                   "Loose scree, faster",
                   "I cut across the benches",
                   "Nothing stops on this grade",
                   "I run the haul road"],
               "brute": [
                   "Boulders break easier",
                   "I pull the face down",
                   "Granite is soft enough",
                   "I carry the overburden",
                   "The drill broke on me",
                   "I am the blast"],
               "calm": [
                   "Watch your footing here.",
                   "Blasting finished hours ago.",
                   "That ledge is not stable.",
                   "Hard hat, even down here.",
                   "The pit floods after rain.",
                   "Trucks stopped running last season."]}},
    {"name": "Dune Crawl", "biome": "desert", "count_scale": 1.3, "speed_scale": 1.0,
     "detect_scale": 1.5, "brute_bias": 0.15, "hp_bonus": 1,
     "barks": {
               "walker": [
                   "Sand in everything",
                   "I walked since morning",
                   "My canteen ran dry",
                   "The dunes keep moving",
                   "Nothing grows where I stepped",
                   "Heat took my shadow"],
               "runner": [
                   "No shade out here",
                   "I cross before noon",
                   "The sand does not slow me",
                   "Faster than the dust devil",
                   "I follow the ridge line",
                   "Downwind and closing"],
               "brute": [
                   "The heat feeds me",
                   "I flatten the dunes",
                   "Stone melts, I do not",
                   "I carry the sun",
                   "Nothing in my way stands",
                   "I am the sandstorm"],
               "calm": [
                   "Water's two ridges east.",
                   "Sun'll cook you by noon.",
                   "Travel at dusk, not now.",
                   "Cover your neck out here.",
                   "The old well still works.",
                   "Follow the cairns back."]}},
    {"name": "Tidal Shallows", "biome": "water", "count_scale": 0.9, "speed_scale": 0.7,
     "detect_scale": 1.1, "brute_bias": 0.2, "hp_bonus": 2,
     "barks": {
               "walker": [
                   "Salt in the lungs",
                   "The tide brought me back",
                   "Barnacles on my hands",
                   "I waded out too far",
                   "Kelp around my ankles",
                   "The water is always cold"],
               "runner": [
                   "The tide is faster",
                   "I skim the shallows",
                   "Before the water turns",
                   "Across the sandbar",
                   "I outrun the swell",
                   "Spray behind me"],
               "brute": [
                   "I came in on the surf",
                   "Breakers part for me",
                   "I drag the anchor",
                   "Hulls split on me",
                   "The deep sent me",
                   "I am the undertow"],
               "calm": [
                   "Tide turns in an hour.",
                   "Nets came up empty again.",
                   "Don't swim past the marker.",
                   "Boat's been leaking all week.",
                   "Gulls know where the fish are.",
                   "Storm's coming in from the west."]}},
    {"name": "Snowline Patrol", "biome": "snow", "count_scale": 1.0, "speed_scale": 0.75,
     "detect_scale": 0.9, "brute_bias": 0.25, "hp_bonus": 2,
     "barks": {
               "walker": [
                   "Frost got my fingers",
                   "I sank to the knees",
                   "The drifts hide everything",
                   "My breath froze first",
                   "Tracks fill in behind me",
                   "I stopped feeling cold"],
               "runner": [
                   "Downhill is easy",
                   "I follow the ski line",
                   "Powder does not slow me",
                   "Faster on the crust",
                   "Between the pines",
                   "I beat the whiteout"],
               "brute": [
                   "I am the avalanche",
                   "Ice cracks under me",
                   "I pull the cornice down",
                   "Trees snap in the cold",
                   "The mountain moves for me",
                   "I bring the slide"],
               "calm": [
                   "Storm coming off the ridge.",
                   "Keep your hands covered.",
                   "The pass closed this morning.",
                   "Don't go above the treeline.",
                   "Hut's a mile that way.",
                   "Snow's been falling since dawn."]}},
    {"name": "Understory Ambush", "biome": "forest", "count_scale": 1.4, "speed_scale": 1.1,
     "detect_scale": 0.8, "brute_bias": 0.1, "hp_bonus": 0,
     "barks": {
               "walker": [
                   "Branches hide us",
                   "Roots caught my feet",
                   "Moss grows on me now",
                   "I lost the path",
                   "The canopy keeps it dark",
                   "Something rustles behind"],
               "runner": [
                   "Through the undergrowth",
                   "I know every deer trail",
                   "Ferns part for me",
                   "No trail needed",
                   "I close from the treeline",
                   "Quiet until I am near"],
               "brute": [
                   "Trees fall for me",
                   "I snap the deadwood",
                   "The trunk gives way",
                   "I clear my own path",
                   "Bark splits under my hands",
                   "I am the windfall"],
               "calm": [
                   "Stay on the path.",
                   "Something moved in the ferns.",
                   "Mushrooms here are not safe.",
                   "Light goes fast under the canopy.",
                   "The stream is that way.",
                   "I marked the trees in blue."]}},
    {"name": "Kerbside Rush", "biome": "street", "count_scale": 1.5, "speed_scale": 1.2,
     "detect_scale": 1.2, "brute_bias": 0.2, "hp_bonus": 1,
     "barks": {
               "walker": [
                   "Cross at the lights",
                   "I waited for the signal",
                   "Shop windows are all broken",
                   "My shift never ended",
                   "The bus stopped coming",
                   "Glass under every step"],
               "runner": [
                   "Traffic never stops",
                   "Down the centre line",
                   "I take the alleys",
                   "Faster than the crossing light",
                   "Between the parked cars",
                   "I cut through the arcade"],
               "brute": [
                   "I total cars",
                   "The kerb breaks first",
                   "I fold the shutters",
                   "Lamp posts bend easy",
                   "Nothing parked stays parked",
                   "I clear the lane"],
               "calm": [
                   "Lights haven't worked for weeks.",
                   "Nobody drives here now.",
                   "The corner shop still opens.",
                   "Keep to the middle of the road.",
                   "I heard something two streets over.",
                   "Power's been out since Tuesday."]}},
    {"name": "Silo Shadow", "biome": "farm", "count_scale": 1.2, "speed_scale": 0.85,
     "detect_scale": 1.0, "brute_bias": 0.25, "hp_bonus": 1,
     "barks": {
               "walker": [
                   "Harvest is late",
                   "I came in from the field",
                   "Mud to my knees",
                   "The gate was left open",
                   "Chaff in my throat",
                   "I worked this land"],
               "runner": [
                   "Through the rows",
                   "Faster than the combine",
                   "I know the irrigation lines",
                   "Across the stubble",
                   "Before the dogs wake",
                   "Down the tractor ruts"],
               "brute": [
                   "I pull the plough",
                   "Fences are string to me",
                   "I tip the trailer",
                   "The silo shakes",
                   "Barn doors come off",
                   "I am the thresher"],
               "calm": [
                   "Harvest went bad this year.",
                   "Dog ran off that way.",
                   "Mind the bull in that field.",
                   "Rain would save the crop.",
                   "The tractor's been dead since spring.",
                   "Gate stays shut, please."]}},
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
