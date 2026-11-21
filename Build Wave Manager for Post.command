#!/bin/zsh

cd "$(dirname "$0")" || exit 1

echo ""
echo "=========================================="
echo "  Wave Manager for Post - Build"
echo "=========================================="
echo ""

echo "Python:"
python3 --version

echo ""
echo "PyInstaller:"
python3 -m PyInstaller --version

echo ""
# Only this app's own outputs — Wave Manager and Wave Repair share build/
# and dist/, so wiping the whole folders would delete the other app's build.
echo "Cleaning old Wave Manager for Post build..."
rm -rf "build/Wave Manager for Post" "dist/Wave Manager for Post" "dist/Wave Manager for Post.app"

echo ""
echo "Building Wave Manager for Post.app..."
echo ""

python3 -m PyInstaller --clean --noconfirm "Wave Manager for Post.spec"

if [ $? -eq 0 ]; then
    echo ""
    echo "=========================================="
    echo "  BUILD SUCCESSFUL"
    echo "=========================================="
    echo ""
    echo "Created:"
    echo "dist/Wave Manager for Post.app"
    echo ""

    open "dist"

    echo "Press any key to close..."
    read -k 1
else
    echo ""
    echo "=========================================="
    echo "  BUILD FAILED"
    echo "=========================================="
    echo ""
    echo "Press any key to close..."
    read -k 1
fi
