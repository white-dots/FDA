#!/bin/bash
# FDA System Installation Script for macOS/Linux
# Usage: curl -sSL https://raw.githubusercontent.com/white-dots/FDA/main/install.sh | bash

set -e

echo "=================================="
echo "FDA System Installer"
echo "=================================="
echo ""

# Detect OS
OS="$(uname -s)"
case "${OS}" in
    Linux*)     PLATFORM=Linux;;
    Darwin*)    PLATFORM=Mac;;
    *)          PLATFORM="UNKNOWN:${OS}"
esac

echo "Detected platform: $PLATFORM"
echo ""

# Check Python version
check_python() {
    if command -v python3.12 &> /dev/null; then
        PYTHON_CMD=python3.12
    elif command -v python3.11 &> /dev/null; then
        PYTHON_CMD=python3.11
    elif command -v python3.10 &> /dev/null; then
        PYTHON_CMD=python3.10
    elif command -v python3.9 &> /dev/null; then
        PYTHON_CMD=python3.9
    elif command -v python3 &> /dev/null; then
        PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
        MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 9 ]; then
            PYTHON_CMD=python3
        else
            echo "Error: Python 3.9+ required. Found Python $PYTHON_VERSION"
            exit 1
        fi
    else
        echo "Error: Python 3 not found. Please install Python 3.9 or higher."
        if [ "$PLATFORM" = "Mac" ]; then
            echo ""
            echo "Install with Homebrew:"
            echo "  brew install python@3.12"
        elif [ "$PLATFORM" = "Linux" ]; then
            echo ""
            echo "Install with:"
            echo "  sudo apt install python3.12 python3.12-venv"
        fi
        exit 1
    fi
}

check_python
echo "Using Python: $PYTHON_CMD ($($PYTHON_CMD --version))"
echo ""

# Set install directory
INSTALL_DIR="${FDA_INSTALL_DIR:-$HOME/FDA}"

# Clone or update repository
if [ -d "$INSTALL_DIR" ]; then
    echo "Updating existing installation at $INSTALL_DIR..."
    cd "$INSTALL_DIR"
    git pull
else
    echo "Cloning FDA repository to $INSTALL_DIR..."
    git clone https://github.com/white-dots/FDA.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

echo ""

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    $PYTHON_CMD -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip

# Install FDA with all dependencies
echo "Installing FDA and dependencies..."
pip install -e ".[all]"

echo ""
echo "=================================="
echo "Installation Complete!"
echo "=================================="
echo ""
echo "To get started:"
echo ""
echo "  1. Activate the environment:"
echo "     cd $INSTALL_DIR"
echo "     source venv/bin/activate"
echo ""
echo "  2. Start the setup server:"
echo "     fda setup"
echo ""
echo "  3. Open http://localhost:9999 in your browser"
echo ""
echo "For more commands, run: fda --help"
echo ""

# Add to PATH suggestion
SHELL_RC=""
if [ -f "$HOME/.zshrc" ]; then
    SHELL_RC="$HOME/.zshrc"
elif [ -f "$HOME/.bashrc" ]; then
    SHELL_RC="$HOME/.bashrc"
fi

if [ -n "$SHELL_RC" ]; then
    echo "Optional: Add FDA to your PATH by adding this to $SHELL_RC:"
    echo ""
    echo "  # FDA System"
    echo "  export PATH=\"$INSTALL_DIR/venv/bin:\$PATH\""
    echo ""
fi
