#!/bin/bash
set -e

echo "===================================================="
echo "            STREAMHOME AUTOMATED SETUP WIZARD"
echo "===================================================="
echo ""

# Helper function to check and install missing dependencies
check_and_install() {
    MISSING_DEPS=()
    
    # Check for python3
    if ! command -v python3 >/dev/null 2>&1; then
        MISSING_DEPS+=("python3")
    fi
    
    # Check for pip3
    if ! command -v pip3 >/dev/null 2>&1 && ! python3 -m pip --version >/dev/null 2>&1; then
        # On some systems pip is python3-pip
        if [[ "$OSTYPE" == "linux-gnu"* ]]; then
            MISSING_DEPS+=("python3-pip")
        fi
    fi
    
    # Check for node
    if ! command -v node >/dev/null 2>&1; then
        MISSING_DEPS+=("nodejs")
    fi
    
    # Check for npm
    if ! command -v npm >/dev/null 2>&1; then
        # On brew/mac node includes npm, but on linux apt it might be separate
        if [[ "$OSTYPE" == "linux-gnu"* ]]; then
            MISSING_DEPS+=("npm")
        fi
    fi
    
    # Check for ffmpeg
    if ! command -v ffmpeg >/dev/null 2>&1; then
        MISSING_DEPS+=("ffmpeg")
    fi
    
    # Check for rclone
    if ! command -v rclone >/dev/null 2>&1; then
        MISSING_DEPS+=("rclone")
    fi
    
    if [ ${#MISSING_DEPS[@]} -eq 0 ]; then
        echo "All core system dependencies (Python, Node, FFmpeg, Rclone) are already installed!"
        return 0
    fi
    
    echo "Missing dependencies detected: ${MISSING_DEPS[*]}"
    echo "Installing missing dependencies..."
    
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if [ -x "$(command -v apt-get)" ]; then
            echo "Using APT package manager to install..."
            sudo apt-get update
            sudo apt-get install -y "${MISSING_DEPS[@]}"
        elif [ -x "$(command -v yum)" ]; then
            echo "Using YUM package manager to install..."
            sudo yum check-update || true
            # Ensure epel-release is installed for ffmpeg/rclone on older RHEL/CentOS
            if [[ " ${MISSING_DEPS[*]} " == *" ffmpeg "* ]] || [[ " ${MISSING_DEPS[*]} " == *" rclone "* ]]; then
                sudo yum install -y epel-release || true
            fi
            sudo yum install -y "${MISSING_DEPS[@]}"
        else
            echo "Error: Supported package manager (apt/yum) not found. Please install missing tools manually: ${MISSING_DEPS[*]}"
            exit 1
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        if [ -x "$(command -v brew)" ]; then
            echo "Using Homebrew to install..."
            for dep in "${MISSING_DEPS[@]}"; do
                case "$dep" in
                    python3-pip) continue ;; # installed with python
                    nodejs) brew install node ;;
                    python3) brew install python ;;
                    *) brew install "$dep" ;;
                esac
            done
        else
            echo "Homebrew is missing. Installing Homebrew first..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            eval "$(/opt/homebrew/bin/brew shellenv)"
            for dep in "${MISSING_DEPS[@]}"; do
                case "$dep" in
                    python3-pip) continue ;;
                    nodejs) brew install node ;;
                    python3) brew install python ;;
                    *) brew install "$dep" ;;
                esac
            done
        fi
    else
        echo "Unsupported OS type: $OSTYPE. Please install missing dependencies manually: ${MISSING_DEPS[*]}"
        exit 1
    fi
}

# 1. Run the dependency checks
check_and_install

# 2. Setup Virtual Environment and Install requirements
echo ""
echo "Setting up Python virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
echo "Activating virtual environment..."
source venv/bin/activate

echo "Installing Server Python dependencies inside virtual environment..."
pip install --upgrade pip
pip install -r server/requirements.txt

echo ""
echo "Installing Web Client Node dependencies..."
cd web
npm install
cd ..

# 3. Grant execute permissions to start/stop scripts
chmod +x start.sh start_background.sh stop.sh setup.sh || true

echo ""
echo "===================================================="
echo "       DEPENDENCIES ARE SUCCESSFULLY INSTALLED"
echo "           LAUNCHING SETUP CONFIGURATION..."
echo "===================================================="
echo ""
python3 server/cli.py --setup
