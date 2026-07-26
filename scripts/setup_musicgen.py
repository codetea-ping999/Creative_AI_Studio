#!/usr/bin/env python3
"""Reorganize MusicGen model layout."""

from pathlib import Path
import shutil
import sys

def setup_musicgen_model():
    target_base = Path("models/audio/musicgen-small")
    cache_dir = target_base / "models--facebook--musicgen-small/snapshots" 
    
    snapshots = list(cache_dir.glob("*/"))
    if not snapshots:
        print("❌ No snapshots found!")
        return False
    
    snapshot_dir = snapshots[0]
    print(f"📦 Found snapshot: {snapshot_dir.name}")
    
    # Copy files to root
    print("📋 Reorganizing files...")
    for item in snapshot_dir.iterdir():
        dest = target_base / item.name
        
        # Remove existing
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        
        # Copy new
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
    
    # Clean cache structure
    cache_parent = target_base / "models--facebook--musicgen-small"
    if cache_parent.exists():
        shutil.rmtree(cache_parent)
    locks = target_base / ".locks"
    if locks.exists():
        shutil.rmtree(locks)
    
    print("✅ MusicGen model structured correctly!")
    print("\n📂 Model contents:")
    for f in sorted(target_base.glob("*")):
        if f.name.startswith("."):
            continue
        if f.is_dir():
            item_count = len(list(f.rglob("*")))
            print(f"  📁 {f.name}/ ({item_count} items)")
        else:
            size_mb = f.stat().st_size / (1024**2)
            print(f"  📄 {f.name} ({size_mb:.1f}MB)")
    
    return True

if __name__ == "__main__":
    success = setup_musicgen_model()
    sys.exit(0 if success else 1)
