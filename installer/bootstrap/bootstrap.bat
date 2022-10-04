@echo off

@rem This file initializes micromamba and activates the env.
@rem A similar bootstrap file needs to exist for each platform (win, linux, macOS)
@rem Ready to hand-over to the platform-independent installer after this (written in python).

set MAMBA_ROOT_PREFIX=%SD_BASE_DIR%\env\mamba
set INSTALL_ENV_DIR=%SD_BASE_DIR%\env\installer_env
set INSTALLER_YAML_FILE=%SD_BASE_DIR%\installer\yaml\installer-environment.yaml
set MICROMAMBA_BINARY_FILE=%SD_BASE_DIR%\installer\bin\micromamba_win_x64.exe

@rem initialize the mamba dir
if not exist "%MAMBA_ROOT_PREFIX%" mkdir "%MAMBA_ROOT_PREFIX%"

copy "%MICROMAMBA_BINARY_FILE%" "%MAMBA_ROOT_PREFIX%\micromamba.exe"

@rem test the mamba binary
echo Micromamba version:
call "%MAMBA_ROOT_PREFIX%\micromamba.exe" --version

@rem run the shell hook
if not exist "%MAMBA_ROOT_PREFIX%\Scripts" (
    call "%MAMBA_ROOT_PREFIX%\micromamba.exe" shell hook --log-level 4 -s cmd.exe
)

call "%MAMBA_ROOT_PREFIX%\condabin\mamba_hook.bat"

@rem create the installer env
if not exist "%INSTALL_ENV_DIR%" (
    call micromamba create -y --prefix "%INSTALL_ENV_DIR%" -f "%INSTALLER_YAML_FILE%"
)

@rem activate
call micromamba activate "%INSTALL_ENV_DIR%"
