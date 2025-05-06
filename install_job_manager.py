import os
import sys
import subprocess
import tempfile
import shutil
import traceback
import datetime
import time
import stat
from pathlib import Path

DEBUG = False
JOB_MANAGER_REPO_URL = "https://github.com/axel-devs/VisoMaster-Job-Manager.git"
STOCK_REPO_URL = "https://github.com/visomaster/VisoMaster.git"
HANS_EXPERIMENTAL_REPO_URL = "https://github.com/asdf31jsa/VisoMaster-Experimental.git"
JOB_MANAGER_BRANCH = "dev"
STOCK_BRANCH = "main"
HANS_EXPERIMENTAL_BRANCH = "ALL_Working"
TARGET_ITEMS = ["app", "main.py"]
HANS_TARGET_FILE_RELATIVE = os.path.join("app", "processors", "video_processor.py")
TEMP_PREFIX = "visomaster_updater_temp_"
STOCK_VS_JM_PATCH_FILENAME = "job-manager_vs_stock.patch"

def print_debug(*args, **kwargs):
    if DEBUG:
        print("[DEBUG]", *args, **kwargs)

def quote_path(path):
    if ' ' in path and not (path.startswith('"') and path.endswith('"')):
        return f'"{path}"'
    return path

def run_command(command, cwd=None, suppress_output=False, check=True):
    command_str = ' '.join(quote_path(part) for part in command)
    print_debug(f"Running: {command_str} {'(in '+str(cwd)+')' if cwd else ''}")
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )

        stdout_content = result.stdout if result.stdout else ""
        stderr_content = result.stderr if result.stderr else ""
        exit_code = result.returncode

        if DEBUG:
            if stderr_content:
                is_progress = stderr_content.strip().startswith(('Receiving objects', 'Resolving deltas', 'Updating files', 'remote:', 'Cloning into'))
                if not is_progress or exit_code != 0:
                    for line in stderr_content.strip().splitlines():
                        print_debug(f"  stderr: {line}")
            if not suppress_output and stdout_content:
                for line in stdout_content.strip().splitlines():
                    print_debug(f"  stdout: {line}")

        if check and exit_code != 0:
            raise subprocess.CalledProcessError(exit_code, command, output=stdout_content, stderr=stderr_content)

        success = (exit_code == 0)
        return success, exit_code
    except FileNotFoundError:
        print(f"Error: Required program '{command[0]}' not found. Is it installed and accessible?")
        return False, -1
    except subprocess.CalledProcessError as e:
        print_debug(f"Error: Command failed with exit code {e.returncode}")
        if DEBUG:
            stderr_lines_err = e.stderr.strip().splitlines() if e.stderr else []
            stdout_lines_err = e.stdout.strip().splitlines() if e.stdout else []
            if stderr_lines_err:
                for line in stderr_lines_err:
                    print_debug(f"  stderr: {line}")
            if stdout_lines_err:
                for line in stdout_lines_err:
                    print_debug(f"  stdout: {line}")
        return False, e.returncode
    except Exception as e:
        print(f"An unexpected error occurred while running command.")
        print_debug(f"Command: {command_str}")
        print_debug(f"Error details: {e}")
        if DEBUG:
            traceback.print_exc()
        return False, -1

def fetch_repo_files(repo_url, branch_name, target_dir, items_to_fetch=None):
    repo_basename = os.path.basename(repo_url)
    print_debug(f"Fetching branch '{branch_name}' from {repo_basename} into {target_dir}...")
    print(f"Downloading files from {repo_basename} ({branch_name})...")
    abs_target_dir = os.path.abspath(target_dir)
    parent_dir = os.path.dirname(abs_target_dir)
    target_name = os.path.basename(abs_target_dir)
    print_debug(f"Normalized target directory: {abs_target_dir}")
    print_debug(f"Parent directory for clone cwd: {parent_dir}")
    print_debug(f"Target name for clone: {target_name}")
    try:
        os.makedirs(parent_dir, exist_ok=True)
    except OSError as e:
        print(f"Error: Could not create parent directory for clone: {parent_dir}")
        print_debug(f"OSError: {e}")
        return False
    if os.path.lexists(abs_target_dir):
       print_debug(f"Removing existing target before clone: {abs_target_dir}")
       try:
           if os.path.isdir(abs_target_dir) and not os.path.islink(abs_target_dir):
               shutil.rmtree(abs_target_dir, ignore_errors=False, onerror=handle_remove_readonly)
               if os.path.exists(abs_target_dir): 
                   print_debug(f"Initial rmtree failed for pre-clone cleanup of {abs_target_dir}. Retrying...")
                   time.sleep(0.5)
                   shutil.rmtree(abs_target_dir, ignore_errors=True) 
           elif os.path.isfile(abs_target_dir) or os.path.islink(abs_target_dir):
               os.remove(abs_target_dir)
           if os.path.exists(abs_target_dir):
                raise OSError(f"Failed to remove existing item after retries: {abs_target_dir}")
       except OSError as e:
           print(f"Error: Could not remove existing item at target path: {abs_target_dir}")
           print_debug(f"Removal error: {e}")
           return False
    clone_command = [
        "git", "clone",
        "--branch", branch_name,
        "--depth", "1",
        "--no-tags",
        "--no-recurse-submodules",
        repo_url,
        target_name
    ]
    clone_success, clone_exit_code = run_command(clone_command, cwd=parent_dir, suppress_output=not DEBUG, check=False)
    if not clone_success or clone_exit_code != 0:
        print(f"Error: Failed to download required files from {repo_basename}.")
        print_debug(f"Failed to clone branch '{branch_name}' from repository {repo_url} into {abs_target_dir}. Exit Code: {clone_exit_code}")
        if clone_exit_code == 128:
             print_debug(f"Hint: Branch '{branch_name}' might not exist, repository access issue, network problems, or target folder name conflict.")
        return False
    if not os.path.isdir(abs_target_dir):
       print(f"Error: Git clone command finished but target directory is missing: {abs_target_dir}")
       print_debug("This might indicate a failure during checkout (e.g., permissions, filesystem issue) even though Git exited cleanly.")
       return False
    if items_to_fetch:
        for item in items_to_fetch:
            expected_path = os.path.join(abs_target_dir, item)
            if not os.path.exists(expected_path):
                print_debug(f"Warning: Expected item '{item}' not found in downloaded {repo_basename} (branch: {branch_name}).")
    print_debug(f"Successfully fetched branch '{branch_name}' from {repo_url}.")
    return True

def create_patch_file(base_dir, target_dir, patch_file_path, items_to_diff):
    base_repo_name = os.path.basename(base_dir)
    target_repo_name = os.path.basename(target_dir)
    patch_filename = os.path.basename(patch_file_path)
    print_debug(f"Generating patch '{patch_filename}' ({target_repo_name} vs {base_repo_name}) focusing on {items_to_diff}")
    print(f"Preparing update (comparing {target_repo_name} against {base_repo_name})...")
    parent_temp_dir = os.path.dirname(base_dir)
    filtered_base_name = base_repo_name + "_filtered"
    filtered_target_name = target_repo_name + "_filtered"
    filtered_base_dir = os.path.join(parent_temp_dir, filtered_base_name)
    filtered_target_dir = os.path.join(parent_temp_dir, filtered_target_name)
    try:
        shutil.rmtree(filtered_base_dir, ignore_errors=True, onerror=handle_remove_readonly)
        shutil.rmtree(filtered_target_dir, ignore_errors=True, onerror=handle_remove_readonly)
        os.makedirs(filtered_base_dir, exist_ok=True)
        os.makedirs(filtered_target_dir, exist_ok=True)
        print_debug("Creating temporary filtered directories by copying target items...")
        for item in items_to_diff:
            src_base_item = os.path.join(base_dir, item)
            src_target_item = os.path.join(target_dir, item)
            dest_base_item = os.path.join(filtered_base_dir, item)
            dest_target_item = os.path.join(filtered_target_dir, item)
            if os.path.exists(src_base_item):
                if os.path.isdir(src_base_item):
                    shutil.copytree(src_base_item, dest_base_item, dirs_exist_ok=True)
                elif os.path.isfile(src_base_item):
                    os.makedirs(os.path.dirname(dest_base_item), exist_ok=True)
                    shutil.copy2(src_base_item, dest_base_item)
                print_debug(f"Copied '{item}' from base ({base_repo_name}) to filtered base.")
            else:
                print_debug(f"Item '{item}' not found in base directory '{base_repo_name}', skipping for filtered diff.")
            if os.path.exists(src_target_item):
                if os.path.isdir(src_target_item):
                    shutil.copytree(src_target_item, dest_target_item, dirs_exist_ok=True)
                elif os.path.isfile(src_target_item):
                    os.makedirs(os.path.dirname(dest_target_item), exist_ok=True)
                    shutil.copy2(src_target_item, dest_target_item)
                print_debug(f"Copied '{item}' from target ({target_repo_name}) to filtered target.")
            else:
                print_debug(f"Item '{item}' not found in target directory '{target_repo_name}', skipping for filtered diff.")
    except Exception as e:
        print(f"Error preparing comparison files during copy.")
        print_debug(f"Error creating or populating filtered directories: {e}")
        shutil.rmtree(filtered_base_dir, ignore_errors=True, onerror=handle_remove_readonly)
        shutil.rmtree(filtered_target_dir, ignore_errors=True, onerror=handle_remove_readonly)
        return False, False
    abs_patch_file_path = os.path.abspath(patch_file_path)
    diff_command = [
        "git", "diff",
        "--no-index",
        "--binary",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        f"--output={abs_patch_file_path}",
        filtered_base_name,
        filtered_target_name
    ]
    diff_success, diff_exit_code = run_command(diff_command, cwd=parent_temp_dir, suppress_output=not DEBUG, check=False)
    if DEBUG and os.path.exists(abs_patch_file_path):
        try:
            print_debug(f"--- Start of generated patch file ({patch_filename}) ---")
            with open(abs_patch_file_path, 'r', encoding='utf-8', errors='ignore') as pf:
                for i, line in enumerate(pf):
                    if i >= 20:
                        print_debug("    ...")
                        break
                    print_debug(f"PATCH_LINE: {line.rstrip()}")
            print_debug("--- End of generated patch file snippet ---")
        except Exception as e_read:
            print_debug(f"Warning: Could not read patch file for inspection: {e_read}")
    elif DEBUG:
        print_debug(f"Patch file {patch_filename} not found after diff command (Exit Code: {diff_exit_code}).")
    patch_exists_and_has_content = os.path.exists(abs_patch_file_path) and os.path.getsize(abs_patch_file_path) > 0
    if diff_exit_code == 0:
        print(f"Downloaded {target_repo_name} version is identical to {base_repo_name} regarding target files.")
        if os.path.exists(abs_patch_file_path):
            try: os.remove(abs_patch_file_path)
            except Exception as e_rem: print_debug(f"Warning: Could not remove empty patch file: {e_rem}")
        return True, False
    elif diff_exit_code == 1 and patch_exists_and_has_content:
        print_debug(f"Successfully created patch file: {patch_filename}")
        return True, True
    else:
        print(f"Error: File comparison failed (git diff exit code: {diff_exit_code}).")
        if not patch_exists_and_has_content and diff_exit_code == 1:
             print_debug("Reason: Git reported differences, but the patch file is missing or empty.")
        if os.path.exists(abs_patch_file_path):
            try: os.remove(abs_patch_file_path)
            except Exception as e_rem: print_debug(f"Warning: Could not remove potentially incomplete patch file: {e_rem}")
        return False, False

def force_overwrite_with_job_manager(source_repo_dir, target_install_dir):
    print(f"Performing direct overwrite with Job Manager files...")
    print_debug(f"Source (Job Manager temp): {source_repo_dir}")
    print_debug(f"Target (Installation dir): {target_install_dir}")
    errors = False
    items_overwritten = 0
    for item in TARGET_ITEMS:
        src_item_path = os.path.join(source_repo_dir, item)
        dest_item_path = os.path.join(target_install_dir, item)
        if not os.path.exists(src_item_path):
            print(f"Warning: Source item '{item}' not found in downloaded Job Manager repo. Skipping overwrite for this item.")
            continue
        print(f"Overwriting '{item}'...")
        try:
            if os.path.lexists(dest_item_path):
                print_debug(f"Removing existing destination: {dest_item_path}")
                if os.path.isdir(dest_item_path) and not os.path.islink(dest_item_path):
                     shutil.rmtree(dest_item_path, ignore_errors=True, onerror=handle_remove_readonly)
                elif os.path.isfile(dest_item_path) or os.path.islink(dest_item_path):
                     os.remove(dest_item_path)
                if os.path.exists(dest_item_path): 
                    print_debug(f"Removal failed for {dest_item_path}. Retrying...")
                    time.sleep(0.5)
                    if os.path.isdir(dest_item_path) and not os.path.islink(dest_item_path):
                         shutil.rmtree(dest_item_path, ignore_errors=True)
                    else:
                         os.remove(dest_item_path)
                if os.path.exists(dest_item_path):
                    raise OSError(f"Failed to remove existing destination: {dest_item_path}")

            os.makedirs(os.path.dirname(dest_item_path), exist_ok=True)
            print_debug(f"Copying {src_item_path} to {dest_item_path}")
            if os.path.isdir(src_item_path):
                shutil.copytree(src_item_path, dest_item_path, dirs_exist_ok=False)
            elif os.path.isfile(src_item_path):
                shutil.copy2(src_item_path, dest_item_path)
            items_overwritten += 1
        except Exception as e:
            print(f"ERROR overwriting '{item}'.")
            print_debug(f"Error details: {e}")
            errors = True
    if errors:
        print("Errors occurred during the overwrite process.")
        return False
    elif items_overwritten == 0:
        print("Warning: No items were actually overwritten (source items might be missing).")
        return False
    else:
        print("Overwrite completed for specified items.")
        return True

def install_hans_experimental(target_install_dir, temp_dir):
    print_debug("\nPreparing for Hans' Experimental Mod...")
    local_app_dir = os.path.join(target_install_dir, "app")
    hans_temp_checkout = os.path.join(temp_dir, "hans_experimental_checkout")
    hans_app_source = os.path.join(hans_temp_checkout, "app")
    print_debug(f"Removing existing local 'app' directory: {local_app_dir}")
    try:
        if os.path.lexists(local_app_dir):
            if os.path.isdir(local_app_dir) and not os.path.islink(local_app_dir):
                 shutil.rmtree(local_app_dir, ignore_errors=True, onerror=handle_remove_readonly)
                 print_debug("Attempted removal of local 'app' directory.")
            elif os.path.isfile(local_app_dir) or os.path.islink(local_app_dir):
                 os.remove(local_app_dir)
                 print_debug("Successfully removed local 'app' (was file/link).")
            if os.path.exists(local_app_dir): 
                print_debug(f"Removal of {local_app_dir} failed. Retrying...")
                time.sleep(0.5)
                if os.path.isdir(local_app_dir) and not os.path.islink(local_app_dir):
                    shutil.rmtree(local_app_dir, ignore_errors=True)
                else:
                    os.remove(local_app_dir)
            if os.path.exists(local_app_dir):
                 raise OSError(f"Failed to remove existing 'app' dir: {local_app_dir}")
        else:
             print_debug("Local 'app' directory did not exist, nothing to remove.")
    except Exception as e:
        print(f"Error: Failed to remove local 'app' directory before Hans' Mod installation.")
        print_debug(f"Error details: {e}")
        return None
    print_debug(f"Downloading Hans' Experimental files...")
    clone_success = fetch_repo_files(
        HANS_EXPERIMENTAL_REPO_URL,
        HANS_EXPERIMENTAL_BRANCH,
        hans_temp_checkout
    )
    if not clone_success:
        print("Error: Failed to download Hans' Experimental repository.")
        return None
    if not os.path.isdir(hans_app_source):
        print(f"Error: The required 'app' folder was not found in the downloaded Hans' Experimental repository ({HANS_EXPERIMENTAL_BRANCH} branch).")
        print_debug(f"Looked for: {hans_app_source}")
        return None
    print_debug(f"Copying Hans' Experimental 'app' folder to installation directory...")
    try:
        print_debug(f"Copying {hans_app_source} to {local_app_dir}")
        shutil.copytree(hans_app_source, local_app_dir)
        print_debug("Hans' Experimental Mod 'app' directory installed successfully.")
        return hans_temp_checkout
    except Exception as e:
        print(f"Error: Failed to copy Hans' Experimental 'app' folder into place.")
        print_debug(f"Error details: {e}")
        shutil.rmtree(local_app_dir, ignore_errors=True, onerror=handle_remove_readonly)
        return None

def create_backup(source_dir, backup_dir_base):
    app_dir = os.path.join(source_dir, "app")
    main_py = os.path.join(source_dir, "main.py")
    backup_items_exist = os.path.isdir(app_dir) or os.path.isfile(main_py)
    if not backup_items_exist:
        print_debug(f"Backup skipped: Neither 'app' directory nor 'main.py' found in {source_dir}")
        return None
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"visomaster_backup_{timestamp}"
    backup_path = os.path.join(backup_dir_base, backup_name)
    print(f"Creating backup...")
    print_debug(f"Backup destination: {backup_path}")
    try:
        os.makedirs(backup_path, exist_ok=True)
        copied_something = False
        if os.path.isdir(app_dir):
            dest_app_dir = os.path.join(backup_path, "app")
            print_debug(f"Backing up: {app_dir} to {dest_app_dir}")
            shutil.copytree(app_dir, dest_app_dir, symlinks=True, dirs_exist_ok=True)
            copied_something = True
        if os.path.isfile(main_py):
            dest_main_py = os.path.join(backup_path, "main.py")
            print_debug(f"Backing up: {main_py} to {dest_main_py}")
            shutil.copy2(main_py, dest_main_py)
            copied_something = True
        if copied_something:
            print_debug(f"Backup created successfully: {backup_name}")
            return backup_path
        else:
            print_debug("Backup directory created, but no items ('app' or 'main.py') were actually copied.")
            shutil.rmtree(backup_path, ignore_errors=True, onerror=handle_remove_readonly)
            return None
    except Exception as e:
        print(f"Error: Failed to create backup.")
        print_debug(f"Error details: {e}")
        shutil.rmtree(backup_path, ignore_errors=True, onerror=handle_remove_readonly)
        return None

def copy_missing_files(source_root, dest_root, items_to_check=None):
    print_debug("Ensuring all required files are present...")
    print_debug(f"Source root for missing file check: {source_root}")
    print_debug(f"Destination root for missing file check: {dest_root}")
    if not os.path.isdir(source_root):
        print_debug(f"Warning: Source directory '{source_root}' not found for missing file check. Skipping.")
        return True
    if not os.path.isdir(dest_root):
        print(f"Error: Destination directory '{dest_root}' not found. Cannot copy missing files.")
        return False
    copied_count = 0
    errors = False
    check_items = items_to_check if items_to_check else [d for d in os.listdir(source_root) if d not in ['.git', '__pycache__']]
    print_debug(f"Items to check/copy if missing: {check_items}")
    for item_name in check_items:
        src_path = os.path.join(source_root, item_name)
        dest_path = os.path.join(dest_root, item_name)
        if not os.path.lexists(src_path):
            print_debug(f"Source item '{item_name}' doesn't exist, skipping.")
            continue
        if not os.path.lexists(dest_path):
            copied_count += 1
            print_debug(f"Copying missing item: {item_name}")
            try:
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                if os.path.isdir(src_path):
                    shutil.copytree(src_path, dest_path, symlinks=True)
                elif os.path.isfile(src_path):
                    shutil.copy2(src_path, dest_path)
                else:
                     print_debug(f"Warning: Source item '{item_name}' is neither file nor directory? Skipping.")
            except Exception as e:
                print(f"ERROR copying missing item '{item_name}': {e}")
                errors = True
    if copied_count > 0:
        print(f"Copied {copied_count} missing top-level item(s) from the update source.")
    else:
        print_debug("No missing top-level items needed to be copied.")
    return not errors

def apply_patch(patch_content, patch_description, target_dir, strip_level=1):
    print(f"Attempting to apply patch...")
    patch_file_path = None
    try:
        fd, patch_file_path = tempfile.mkstemp(suffix=".patch", text=True)
        print_debug(f"Created temporary patch file: {patch_file_path}")
        with os.fdopen(fd, 'w', encoding='utf-8') as temp_patch_file:
            temp_patch_file.write(patch_content)
        abs_patch_file_path = os.path.abspath(patch_file_path)
        apply_base_command = [
            "git", "apply",
            "--reject",
            "--ignore-space-change",
            "--ignore-whitespace",
            f"-p{strip_level}",
            abs_patch_file_path
        ]
        apply_check_command = apply_base_command[:2] + ["--check"] + apply_base_command[2:]
        print_debug(f"Running apply check: {' '.join(quote_path(p) for p in apply_check_command)}")
        check_success, check_exit_code = run_command(apply_check_command, cwd=target_dir, suppress_output=False, check=False)
        if check_success and check_exit_code == 0:
            print_debug(f"Patch compatibility check successful for {patch_description}. Applying...")
            apply_command = apply_base_command
            print_debug(f"Running apply: {' '.join(quote_path(p) for p in apply_command)}")
            apply_success, apply_exit_code = run_command(apply_command, cwd=target_dir, suppress_output=False, check=False)
            if apply_success and apply_exit_code == 0:
                print(f"Patch '{patch_description}' applied successfully.")
                return True
            else:
                print(f"Error: Patch application failed for {patch_description} (exit code {apply_exit_code}).")
                print("Conflicts likely exist. Look for '.rej' files for details.")
                return False
        else:
            print(f"Patch compatibility check failed for {patch_description}.")
            print_debug(f"Apply check failed (exit code {check_exit_code}). Conflicts likely exist.")
            return False
    except Exception as e:
        print(f"An unexpected error occurred during patch application for {patch_description}.")
        print_debug(f"Error details: {e}")
        return False
    finally:
        if patch_file_path and os.path.exists(patch_file_path):
            try:
                os.remove(patch_file_path)
                print_debug(f"Removed temporary patch file: {patch_file_path}")
            except OSError as e_rem:
                print_debug(f"Warning: Could not remove temporary patch file {patch_file_path}: {e_rem}")

def handle_remove_readonly(func, path, exc_info):
    excvalue = exc_info[1]
    if func in (os.rmdir, os.remove, os.unlink) and isinstance(excvalue, PermissionError):
        print_debug(f"PermissionError deleting {path}. Attempting to change permissions.")
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception as e:
            print_debug(f"Could not change permissions or delete {path} after chmod: {e}")
    else:
        print_debug(f"Unhandled error during rmtree: func={func}, path={path}")
        raise excvalue

def safe_rmtree(path, max_retries=3, delay=1):
    for attempt in range(max_retries):
        try:
            shutil.rmtree(path, ignore_errors=False, onerror=handle_remove_readonly)
            if not os.path.exists(path):
                print_debug(f"Successfully removed directory: {path}")
                return True
            else:
                print_debug(f"Directory still exists after rmtree attempt {attempt + 1}: {path}")
        except Exception as e:
            print_debug(f"Error removing directory {path} on attempt {attempt + 1}: {e}")

        if attempt < max_retries - 1:
            print_debug(f"Retrying removal in {delay} seconds...")
            time.sleep(delay)
        else:
            print(f"Warning: Could not completely remove temp dir after {max_retries} attempts: {path}")
            print_debug(f"Final attempt to remove {path} failed.")
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception as final_e:
                 print_debug(f"Final ignore_errors=True rmtree also failed: {final_e}")
            return False
    return False

def main():
    print("""
                                                                                                          
                                        @@@@@@@@@@@@@@@@@                                                 
                                    @@@@@@@@@@@@@@@@@@@@@@@@@                                             
                                 @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@                                          
                               @@@@@@@@@          @@@@@@@@@@@@@@@@                                        
                              @@@@@@@                 @@@@@@@@@@@@@@                                      
                             @@@@@@                     @@@@@@@@@@@@                                      
                           @@@@@@@                   @   @@@@@@@@@@@@@                                    
                           @@@@@@                   @@@   @@@@@@@@@@@@                                    
                           @@@@@@                 @@@@@   @@@@@@@@@@@@                                    
                           @@@@@@@               @@@@@@   @@@@@@@@@@@@                                    
                           @@@@@@@              @@@@@@@@ @@@@@@@@@@@@@                                    
                           @@@@@@@@     @     @@@@@@@@@@ @@@@@@@@@@@@@                                    
                           @@@@@@@@@     @@   @@@@@@@@@  @@@@@@@@@@@@@                                    
                             @@@@@@@@@    @@  @@@@@@@@@   @@@@@@@@@@@                                     
                              @@@@@@@@@@@  @@  @@@@@@         @@@@@@                                      
                               @@@@@@@@@@@@@@@@ @@@@@@@@@@@@@@@@@@                                        
                                 @@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@                                          
                                    @@@@@@@@@@@@@@@@@@@@@@@@@                                             
                                        @@@@@@@@@@@@@@@@@                                                 
                                                                                                          
  888888          888            888b     d888                                                            
    "88b          888            8888b   d8888                                                            
     888          888            88888b.d88888                                                            
     888  .d88b.  88888b.        888Y88888P888  8888b.  88888b.   8888b.   .d88b.   .d88b.  888d888       
     888 d88""88b 888 "88b       888 Y888P 888     "88b 888 "88b     "88b d88P"88b d8P  Y8b 888P"         
     888 888  888 888  888       888  Y8P  888 .d888888 888  888 .d888888 888  888 88888888 888           
     88P Y88..88P 888 d88P       888   "   888 888  888 888  888 888  888 Y88b 888 Y8b.     888           
     888  "Y88P"  88888P"        888       888 "Y888888 888  888 "Y888888  "Y88888  "Y8888  888           
   .d88P                                                                       888                        
 .d88P"                                                                   Y8b d88P                        
888P"                                                                      "Y88P"                         
                                                                                                          
                              By Axel | https://github.com/axel-devs                                      
""")
    script_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in locals() else os.getcwd()
    final_exit_code = 1
    hans_mod_installed_or_requested = False
    backup_created_path = None
    temp_dir_path = None
    print_debug(f"\nChecking installation directory: {script_dir}")
    local_app_dir_check = os.path.join(script_dir, "app")
    local_main_py_check = os.path.join(script_dir, "main.py")
    if not os.path.isdir(local_app_dir_check) and not os.path.isfile(local_main_py_check):
        print("\nError: Neither 'app' directory nor 'main.py' found.")
        print("Place this script inside your main VisoMaster installation directory.")
        return 1
    print_debug("Found required VisoMaster files/folders.")
    if not shutil.which("git"):
        print("\nError: 'git' command not found.")
        print("Install Git and ensure it's in your system's PATH.")
        return 1
    print_debug("Found Git installation.")
    print("--- Backup ---")
    backup_created_path = create_backup(script_dir, script_dir)
    if backup_created_path:
        backup_display_name = os.path.basename(backup_created_path)
        print(f"If anything goes wrong, restore from backup: {backup_display_name}")
    else:
        print("Warning: Could not create a backup.")
        while True:
            answer = input("Continue without a backup? Risky! [y/N]: ").lower().strip()
            if answer == 'y': print("Proceeding without backup."); break
            elif answer in ('n', ''): print("Operation cancelled."); return 1
            else: print("Please answer 'y' or 'n'.")
    print("\n--- Hans' Experimental Mod Check ---")
    while True:
        answer = input("Do you currently have Hans' Experimental Mod installed? [y/N]: ").lower().strip()
        if answer == 'y':
            hans_mod_installed_or_requested = True
            break
        elif answer in ('n', ''):
            while True:
                answer_install = input("Would you like to install Hans' Mod _instead_ of stock? [y/N]: ").lower().strip()
                if answer_install == 'y': hans_mod_installed_or_requested = True; break
                elif answer_install in ('n', ''): hans_mod_installed_or_requested = False; break
                else: print("Please answer 'y' or 'n'.")
            break
        else: print("Please answer 'y' or 'n'.")
    if hans_mod_installed_or_requested:
        print("\nDisclaimer: Hans' Mod and Job Manager compatibility is not guaranteed.")
        while True:
            answer_proceed = input("Would you like to install or keep Hans' Mod with Job Manager? [y/N]: ").lower().strip()
            if answer_proceed == 'y': break
            elif answer_proceed in ('n', ''): print("Install cancelled."); return 1
            else: print("Please answer 'y' or 'n'.")
    try:
        temp_parent_dir = script_dir if os.access(script_dir, os.W_OK) else None
        temp_dir_path = tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=temp_parent_dir)
        print_debug(f"Using temporary directory: {temp_dir_path}")
        if hans_mod_installed_or_requested:
            if not install_hans_experimental(script_dir, temp_dir_path):
                print("\nError installing Hans' Mod. Cannot proceed.")
                if backup_created_path: print(f"Restore backup: {os.path.basename(backup_created_path)}")
                raise Exception("Hans' Mod installation failed.")
            print("Hans' Mod installation/update step completed.")
        print("\n--- Downloading Required Files ---")
        job_manager_temp_dir = os.path.join(temp_dir_path, "job-manager_checkout")
        stock_temp_dir = os.path.join(temp_dir_path, "stock_checkout")
        main_patch_file_full_path = os.path.join(temp_dir_path, STOCK_VS_JM_PATCH_FILENAME)
        if not fetch_repo_files(JOB_MANAGER_REPO_URL, JOB_MANAGER_BRANCH, job_manager_temp_dir, TARGET_ITEMS):
            raise Exception("Failed to download Job Manager repository.")
        if not fetch_repo_files(STOCK_REPO_URL, STOCK_BRANCH, stock_temp_dir, TARGET_ITEMS):
            raise Exception("Failed to download Stock VisoMaster repository.")
        print("Downloads complete.")
        if hans_mod_installed_or_requested:
            print_debug("\nPreparing Hans' Mod for Job Manager merge...")
            stock_vp_path = os.path.join(stock_temp_dir, HANS_TARGET_FILE_RELATIVE)
            installed_vp_path = os.path.join(script_dir, HANS_TARGET_FILE_RELATIVE)
            installed_vp_dir = os.path.dirname(installed_vp_path)
            if not os.path.isfile(stock_vp_path):
                print(f"Error: Cannot prepare Hans' Mod. Stock file missing: {stock_vp_path}")
                print("Aborting merge attempt.")
                raise Exception("Stock file needed for Hans prep missing.")
            if not os.path.isfile(installed_vp_path):
                print(f"Warning: Installed Hans' file not found at {installed_vp_path}.")
                print("Assuming it's missing or already stock-like. Skipping overwrite pre-step.")
            else:
                print_debug(f"Overwriting installed '{HANS_TARGET_FILE_RELATIVE}' with stock version...")
                try:
                    os.makedirs(installed_vp_dir, exist_ok=True)
                    shutil.copy2(stock_vp_path, installed_vp_path)
                    print_debug("Overwrite successful. Hans' file now matches stock baseline.")
                except Exception as e:
                    print(f"Error overwriting Hans' file with stock version: {e}")
                    print("Automatic merging may fail. The script will attempt the main patch anyway.")
        print("\n--- Job Manager Update ---")
        patch_op_success, patch_generated = create_patch_file(
            stock_temp_dir,
            job_manager_temp_dir,
            main_patch_file_full_path,
            TARGET_ITEMS
        )
        if not patch_op_success:
            raise Exception("Main patch creation failed.")
        if not patch_generated:
            print(f"\nJob Manager changes seem identical to stock VisoMaster.")
            if hans_mod_installed_or_requested:
                 print("Hans' Mod is installed (and prepped). No further action needed for Job Manager.")
            else:
                 print("No update needed for Job Manager.")
            final_exit_code = 0
        else:
            print_debug(f"\nMain patch file '{STOCK_VS_JM_PATCH_FILENAME}' created.")
            main_patch_content = ""
            try:
                with open(main_patch_file_full_path, 'r', encoding='utf-8') as f:
                    main_patch_content = f.read()
            except Exception as e:
                 raise Exception(f"Failed to read generated main patch file: {e}")
            if not main_patch_content:
                 raise Exception("Main patch file is empty.")
            if apply_patch(main_patch_content, "Job Manager vs Stock", script_dir, strip_level=2):
                 print("Main Job Manager patch applied successfully.")
                 if copy_missing_files(job_manager_temp_dir, script_dir, TARGET_ITEMS):
                     print("Final file synchronization complete.")
                     final_exit_code = 0
                 else:
                     print("Warning: Errors during final file sync after patch."); final_exit_code = 1
            else:
                 print("Error: Main Job Manager patch could not be applied automatically.")
                 final_exit_code = 1
                 print("\n--- Fallback: Direct Overwrite Needed ---")
                 if hans_mod_installed_or_requested:
                     print_debug("Automatic patching failed even after preparing Hans' file.")
                     print_debug("This suggests deeper conflicts or issues.")
                     print("\nTo install Job Manager, a full overwrite of 'app' and 'main.py' is required.")
                     while True:
                         answer_overwrite = input("Overwrite installation with Job Manager files? [y/N]: ").lower().strip()
                         if answer_overwrite == 'y':
                             if force_overwrite_with_job_manager(job_manager_temp_dir, script_dir):
                                 print("Job Manager installed via overwrite.")
                                 if copy_missing_files(job_manager_temp_dir, script_dir, TARGET_ITEMS): final_exit_code = 0
                                 else: print("Errors during final file check after overwrite."); final_exit_code = 1
                             else: print("Job Manager overwrite failed."); final_exit_code = 1
                             break
                         elif answer_overwrite in ('n', ''):
                             print("Job Manager update cancelled.")
                             if backup_created_path: print(f"Restore backup if needed: {os.path.basename(backup_created_path)}")
                             final_exit_code = 1
                             break
                         else: print("Please answer 'y' or 'n'.")
                 else:
                     print("Automatic patching failed (likely due to manual changes).")
                     print("Overwriting 'app' and 'main.py' with Job Manager versions.")
                     print("WARNING: Any manual changes you made WILL BE LOST.")
                     if force_overwrite_with_job_manager(job_manager_temp_dir, script_dir):
                         print("Job Manager installed via overwrite.")
                         if copy_missing_files(job_manager_temp_dir, script_dir, TARGET_ITEMS): final_exit_code = 0
                         else: print("Errors during final file check after overwrite."); final_exit_code = 1
                     else: print("Job Manager overwrite failed."); final_exit_code = 1
    except Exception as e:
        print("\n--- An Unexpected Error Occurred During Update --- ")
        print_debug(f"Error details: {e}")
        if DEBUG: traceback.print_exc()
        final_exit_code = 1
        print("Update process encountered a critical error.")
        if backup_created_path: print(f"Consider restoring backup: {os.path.basename(backup_created_path)}")
    finally:
        if os.path.isfile(os.path.join(script_dir, 'install.dat')) or os.path.isdir(os.path.join(script_dir, 'dependencies', 'Python')):
            print("\n--- Portable version detected! ---")
            print("Running conversion now...")
            subprocess.run(os.path.join(script_dir, 'app', 'ui', 'core', 'convert_portable_ui_to_py.bat'), shell=True, check=False)
            print("Portable conversion complete.")
        print_debug("\n--- Cleanup ---")
        if temp_dir_path and os.path.exists(temp_dir_path):
            print_debug(f"Removing temporary directory: {os.path.basename(temp_dir_path)}")
            print_debug(f"Removing: {temp_dir_path}")
            if not safe_rmtree(temp_dir_path):
                 print(f"Warning: Failed to completely remove temp dir: {temp_dir_path}")
        else: print_debug("No temp dir found or already removed.")
        print("\n--- Finished ---")
        if final_exit_code == 0: print("Update process completed successfully!")
        else:
            print("Update process finished with errors or was cancelled.")
            if backup_created_path and os.path.exists(backup_created_path): print(f"Backup at: {os.path.basename(backup_created_path)}")
        print("\nThank you for using the updater!")
        print("\nConsider starring the repositories on GitHub if you find them useful:")
        print(f" - Job Manager: {JOB_MANAGER_REPO_URL}")
        if hans_mod_installed_or_requested:
            print(f" - Hans' Experimental: {HANS_EXPERIMENTAL_REPO_URL}")
        print(f" - VisoMaster: {STOCK_REPO_URL}")
    return final_exit_code

if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = main()
    except Exception as e:
        print("\n--- A Critical Unexpected Error Occurred --- ")
        print("The script had to stop.")
        print_debug(f"Error details: {e}")
        if DEBUG: traceback.print_exc()
        exit_code = 1
    finally:
        print("\nPress Enter to exit.")
        input()
        sys.exit(exit_code)
