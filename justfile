# PBM restore-hang lab. `just` lists the recipes.

python := `command -v uv >/dev/null 2>&1 && echo "uv run" || echo "python3"`

default:
    @just --list

# Experiment 1: protocol model + fault injection (~30s, stdlib only)
model:
    {{python}} model/pbm_state_machine.py

# Experiment 2: storage-deadline A/B, fail-closed (~10 min; docker optional)
stall:
    cd stall-test && ./run.sh

# Experiment 2 with a shorter hang cutoff (~5 min)
stall-quick:
    cd stall-test && CUTOFF=120 ./run.sh

# Clone PBM v2.11.0, apply both patches, build the patched pbm-agent
build:
    cd stall-test && ./run.sh build

# Experiment 3: build the combined image and start the 3-node RS + MinIO
close-up:
    docker build -t psmdb-pbm:7.0 -f close-phase-test/Dockerfile.psmdb-pbm close-phase-test
    docker compose -f close-phase-test/compose.yml up -d

# Experiment 3: tear down the stack
close-down:
    docker compose -f close-phase-test/compose.yml down -v

# Remove the cached PBM clone and build artifacts
clean:
    rm -rf stall-test/work
