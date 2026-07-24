#!/usr/bin/env python3
"""Download Anime SDXL model (Anything v5)."""

from pathlib import Path
import shutil
from huggingface_hub import snapshot_download

def download_anime_sdxl():
    """Download Anything-v5 anime model."""
    
    # Use a popular anime checkpoint
    # Options:
    # - "andite/anything-v4.0" - stable anime
    # - "Lykon/DreamShaper-8-pruned" - flexible
    # - For pure anime: "detailedhologem/Detailedholocute_Anime" or similar
    
    model_id = "Lykon/AnyLoRA"  # Simplified, compatible with SDXL
    local_path = Path("models/image/anime-sdxl")
    
    print(f"📥 Downloading anime model: {model_id}")
    print(f"💾 Target: {local_path.resolve()}")
    
    try:
        # Download model
        result = snapshot_download(
            model_id,
            cache_dir=str(local_path),
            local_files_only=False,
        )
        
        print("✅ Download initiated")
        
        # Reorganize if needed
        cache_dir = local_path / f"models--{model_id.replace('/', '--')}/snapshots"
        snapshots = list(cache_dir.glob("*/"))
        
        if snapshots:
            snapshot_dir = snapshots[0]
            print(f"📦 Organizing snapshot: {snapshot_dir.name}")
            
            # Copy files to root
            for item in snapshot_dir.iterdir():
                dest = local_path / item.name
                if dest.exists():
                    if dest.is_dir():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink()
                
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
            
            # Clean cache
            cache_parent = local_path / f"models--{model_id.replace('/', '--')}"
            if cache_parent.exists():
                shutil.rmtree(cache_parent)
            locks = local_path / ".locks"
            if locks.exists():
                shutil.rmtree(locks)
        
        print("✅ Anime model setup complete!")
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    import sys
    success = download_anime_sdxl()
    sys.exit(0 if success else 1)
