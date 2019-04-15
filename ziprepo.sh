#!/bin/sh
rm repo.zip
7z a repo.zip * -xr!.* -xr!transformer-lm/*/ -xr!data
7z a repo.zip data/dev-v1.1-processed.json
