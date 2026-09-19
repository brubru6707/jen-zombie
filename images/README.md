# Test images

Drop photos here. The **filename prefix up to the first `_`** is the biome the
harness expects Nemotron to pick, and is what MATCH / MISMATCH is scored
against. Everything after the underscore is free-form.

    desert_dunes.jpg
    snow_slope.jpg
    water_beach_surf.jpg        # beach WITH visible water  -> expect "water"
    desert_beach_drysand.jpg    # beach with NO water       -> expect "desert"

Valid prefixes (the ten biome keys):

    lab  rocky  cave  desert  forest  meadow  snow  water  street  farm

A file whose prefix is not one of those still runs, but is reported as
UNSCORED instead of MATCH/MISMATCH.

Accepted extensions: jpg, jpeg, png, webp, bmp, gif. Full-resolution photos
are fine -- the harness downscales to 640px on the long edge before sending.
