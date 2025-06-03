@echo off
REM 仮想環境をアクティブ化
call "%~dp0venv\Scripts\activate.bat"

REM Pythonスクリプトを実行
python "%~dp0bing_rewards_bot.py"

REM 仮想環境を非アクティブ化 (オプション)
deactivate

pause
