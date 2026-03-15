import os
import subprocess
from pathlib import Path
from tools.base import BaseTool, ToolResult

# Default installation path for Longhorn FX MT5
DEFAULT_MT5_DIR = r"C:\Program Files\LHFX SA MT5 Terminal"
META_EDITOR_EXE = os.path.join(DEFAULT_MT5_DIR, "metaeditor64.exe")
TERMINAL_EXE = os.path.join(DEFAULT_MT5_DIR, "terminal64.exe")

class MT5CompileTool(BaseTool):
    """Compiles an MQL5 source file (.mq5) into an executable (.ex5) using MetaEditor."""
    name = "mt5_compile"
    description = "Compiles an MQL5 source file (.mq5) into an executable (.ex5) using MetaEditor."
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to the .mq5 file to compile."}
        },
        "required": ["file_path"]
    }

    def execute(self, file_path: str, **kwargs) -> ToolResult:
        if not os.path.exists(META_EDITOR_EXE):
            return ToolResult(success=False, output="", error=f"MetaEditor not found at {META_EDITOR_EXE}. Please ensure Longhorn FX MT5 is installed.")
        
        file_path = os.path.abspath(file_path)
        if not os.path.exists(file_path):
            return ToolResult(success=False, output="", error=f"Source file not found: {file_path}")
        
        if not file_path.endswith('.mq5') and not file_path.endswith('.mq4'):
            return ToolResult(success=False, output="", error=f"File must be an .mq5 or .mq4 file: {file_path}")

        # /compile executes compilation in background and writes the log to the same folder
        # format: metaeditor.exe /compile:"path\to\file.mq5" /log
        log_file = file_path.replace('.mq5', '.log').replace('.mq4', '.log')
        
        # Ensure old log is removed so we read the fresh one
        if os.path.exists(log_file):
            try:
                os.remove(log_file)
            except Exception:
                pass

        cmd = [
            META_EDITOR_EXE,
            f'/compile:{file_path}',
            '/log'
        ]
        
        try:
            # MetaEditor compiles silently and exits
            process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
            
            # Read the generated log file
            log_content = ""
            if os.path.exists(log_file):
                with open(log_file, "r", encoding="utf-16", errors="ignore") as f:
                    log_content = f.read()
                # Also try utf-8 if utf-16 resulted in garbage
                if "\x00" in log_content:
                     with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                        log_content = f.read()
            
            # MetaEditor returns 0 on success, 1 on warning, >1 on error
            if process.returncode <= 1 and os.path.exists(file_path.replace('.mq5', '.ex5').replace('.mq4', '.ex4')):
                status = "Success" if process.returncode == 0 else "Success with warnings"
                return ToolResult(success=True, output=f"Compilation {status}.\n\nLog Output:\n{log_content}")
            else:
                return ToolResult(success=False, output="", error=f"Compilation Failed (Exit Code: {process.returncode}).\n\nLog Output:\n{log_content}")
                
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error="Compilation timed out after 30 seconds.")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Error running MetaEditor: {str(e)}")


class MT5BacktestTool(BaseTool):
    """Runs a strategy backtest using the MT5 Strategy Tester by generating a tester.ini file."""
    name = "mt5_backtest"
    description = "Runs a strategy backtest using the MT5 Strategy Tester."
    parameters = {
        "type": "object",
        "properties": {
            "expert_path": {"type": "string", "description": "Relative path of the Expert Advisor inside MQL5\\Experts directory (e.g., 'Moving Average\\Moving Average.ex5')"},
            "symbol": {"type": "string", "description": "The symbol to test on (e.g., EURUSD).", "default": "EURUSD"},
            "period": {"type": "string", "description": "The timeframe (e.g., M1, H1, D1).", "default": "H1"},
            "date_from": {"type": "string", "description": "Start date format YYYY.MM.DD", "default": "2023.01.01"},
            "date_to": {"type": "string", "description": "End date format YYYY.MM.DD", "default": "2024.01.01"},
            "deposit": {"type": "integer", "description": "Initial deposit amount.", "default": 10000},
            "execution_mode": {"type": "integer", "description": "0=Auto, 1=Normal, 2=Random delay.", "default": 0}
        },
        "required": ["expert_path"]
    }

    def execute(
        self, 
        expert_path: str,
        symbol: str = "EURUSD",
        period: str = "H1",
        date_from: str = "2023.01.01",
        date_to: str = "2024.01.01",
        deposit: int = 10000,
        execution_mode: int = 0,
        **kwargs
    ) -> ToolResult:
        if not os.path.exists(TERMINAL_EXE):
            return ToolResult(success=False, output="", error=f"MT5 Terminal not found at {TERMINAL_EXE}.")
        
        # 1. Locate MT5 User Data directory in %APPDATA%
        appdata = os.environ.get("APPDATA")
        terminal_base = os.path.join(appdata, "MetaQuotes", "Terminal")
        terminal_data_dir = None
        
        if os.path.exists(terminal_base):
            for d in os.listdir(terminal_base):
                origin_path = os.path.join(terminal_base, d, "origin.txt")
                if os.path.exists(origin_path):
                    try:
                        with open(origin_path, "r", encoding="utf-16", errors="ignore") as f:
                            origin = f.read().strip()
                        if "\x00" in origin or not origin:
                            with open(origin_path, "r", encoding="utf-8", errors="ignore") as f:
                                origin = f.read().strip()
                        if os.path.normpath(origin) == os.path.normpath(os.path.dirname(TERMINAL_EXE)):
                            terminal_data_dir = os.path.join(terminal_base, d)
                            break
                    except Exception:
                        pass
                        
        if not terminal_data_dir:
            return ToolResult(success=False, output="", error=f"Could not find AppData Data Folder for {TERMINAL_EXE}")
            
        working_dir = Path(os.getcwd())
        ini_path = working_dir / "tester.ini"
        
        # Tell MT5 to output Report.htm. It will save it to terminal_data_dir/Report.htm
        sandbox_report = Path(terminal_data_dir) / "Report.htm"
        local_report = working_dir / "Report.htm"
        
        # Clean up old report
        if sandbox_report.exists():
            sandbox_report.unlink()
        
        # Default tester configuration
        ini_content = f"""
[Tester]
Expert={expert_path}
Symbol={symbol}
Period={period}
Deposit={deposit}
Model=1
FromDate={date_from}
ToDate={date_to}
ForwardMode=0
ExecutionMode={execution_mode}
Optimization=0
Report=Report.htm
ReplaceReport=1
ShutdownTerminal=1
"""
        
        try:
            with open(ini_path, "w") as f:
                f.write(ini_content.strip())
                
            cmd = [TERMINAL_EXE, f"/config:{ini_path}"]
            
            # Run terminal headless (tester config ShutdownTerminal=1 will automatically close it)
            process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
            
            if sandbox_report.exists():
                import shutil
                shutil.copy2(sandbox_report, local_report)
                return ToolResult(success=True, output=f"Backtest completed successfully. Report generated at:\n{local_report}")
            else:
                return ToolResult(success=False, output="", error=f"Backtest executed, but report not found in sandbox ({sandbox_report}). Exit code: {process.returncode}")
                
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="", error="Backtest timed out after 5 minutes.")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Error running MT5 Tester: {str(e)}")
