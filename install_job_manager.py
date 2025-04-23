import os
import sys
import subprocess
import tempfile
import shutil
import traceback
import datetime

DEBUG = False
JOB_MANAGER_REPO_URL = "https://github.com/PronPan/VisoMaster-Job-Manager.git"
STOCK_REPO_URL = "https://github.com/visomaster/VisoMaster.git"
HANS_EXPERIMENTAL_REPO_URL = "https://github.com/asdf31jsa/VisoMaster-Experimental.git"
JOB_MANAGER_BRANCH = "main"
STOCK_BRANCH = "main"
HANS_EXPERIMENTAL_BRANCH = "ALL_Working"
TARGET_ITEMS = ["app", "main.py"]
TEMP_PREFIX = "job-manager_patch_temp_"
PATCH_FILENAME = "job-manager_vs_stock.patch"

def print_debug(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)

def quote_path(path):
    if ' ' in path and not (path.startswith('"') and path.endswith('"')):
        return f'"{path}"'
    return path

def run_command(command, cwd=None, suppress_output=False, check=True):
    command_str = ' '.join(quote_path(part) for part in command)
    print_debug(f"  Running: {command_str} {'(in '+str(cwd)+')' if cwd else ''}")
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=check,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )
        exit_code = result.returncode
        if DEBUG and result.stderr:
            is_progress = result.stderr.strip().startswith(('Receiving objects', 'Resolving deltas', 'Updating files', 'remote:', 'Cloning into'))
            if not is_progress or exit_code != 0:
                for line in result.stderr.strip().splitlines():
                    print_debug(f"    stderr: {line}")
        if DEBUG and not suppress_output and result.stdout:
            for line in result.stdout.strip().splitlines():
                print_debug(f"    stdout: {line}")
        return True, exit_code
    except FileNotFoundError:
        print(f"  Error: Required program '{command[0]}' not found. Is it installed and accessible?")
        return False, -1
    except subprocess.CalledProcessError as e:
        if check:
            print_debug(f"  Error: Command failed with exit code {e.returncode}")
            if DEBUG and e.stderr:
                for line in e.stderr.strip().splitlines():
                    print_debug(f"    stderr: {line}")
            if DEBUG and e.stdout:
                for line in e.stdout.strip().splitlines():
                    print_debug(f"    stdout: {line}")
        return False, e.returncode
    except Exception as e:
        print(f"  An unexpected error occurred while running a command.")
        print_debug(f"  Command: {command_str}")
        print_debug(f"  Error details: {e}")
        if DEBUG:
            traceback.print_exc()
        return False, -1

def fetch_repo_files(repo_url, branch_name, target_dir, items_to_fetch=None):
    print_debug(f"\nFetching branch '{branch_name}' from {repo_url} into {target_dir}...")
    print(f"Downloading files from {os.path.basename(repo_url)} ({branch_name})...")
    abs_target_dir = os.path.abspath(target_dir)
    parent_dir = os.path.dirname(abs_target_dir)
    target_name = os.path.basename(abs_target_dir)
    print_debug(f"  Normalized target directory: {abs_target_dir}")
    print_debug(f"  Parent directory for clone cwd: {parent_dir}")
    print_debug(f"  Target name for clone: {target_name}")
    try:
        os.makedirs(parent_dir, exist_ok=True)
    except OSError as e:
        print(f"Error: Could not create parent directory for clone: {parent_dir}")
        print_debug(f"  OSError: {e}")
        return False
    if os.path.lexists(abs_target_dir):
       print_debug(f"  Removing existing target directory before clone: {abs_target_dir}")
       try:
           shutil.rmtree(abs_target_dir, ignore_errors=False)
       except OSError as e:
           if os.path.isfile(abs_target_dir) or os.path.islink(abs_target_dir):
               try:
                   os.remove(abs_target_dir)
               except OSError as e2:
                   print(f"Error: Could not remove existing file/link at target path: {abs_target_dir}")
                   print_debug(f"  Initial rmtree error (if dir): {e}")
                   print_debug(f"  Subsequent remove error: {e2}")
                   return False
           else:
               print(f"Error: Could not remove existing directory at target path: {abs_target_dir}")
               print_debug(f"  rmtree error: {e}")
               return False
    clone_command = [
        "git", "clone",
        "-b", branch_name,
        "--depth", "1",
        repo_url,
        target_name
    ]
    try:
        script_cwd = os.getcwd()
        print_debug(f"  Script working directory before clone: {script_cwd}")
    except Exception as e:
        print_debug(f"  Warning: Could not get current working directory: {e}")
    clone_success, clone_exit_code = run_command(clone_command, cwd=parent_dir, suppress_output=not DEBUG)
    if clone_success and clone_exit_code == 0 and not os.path.isdir(abs_target_dir):
       print(f"Error: Git clone command finished but target directory is missing: {abs_target_dir}")
       print_debug("  This might indicate a failure during checkout (e.g., permissions, filesystem issue) even though Git exited cleanly.")
       clone_success = False
    if not clone_success:
        print(f"Error: Failed to download required files from {os.path.basename(repo_url)}.")
        print_debug(f"Error: Failed to clone branch '{branch_name}' from repository {repo_url} into {abs_target_dir}.")
        if clone_exit_code == 128:
             print_debug(f"  Hint: Branch '{branch_name}' might not exist, repository access issue, or potentially network problems.")
        if clone_exit_code == 0 and not os.path.isdir(abs_target_dir):
            print_debug(f"  Hint: Check filesystem permissions for the parent directory: {parent_dir}")
        return False
    if items_to_fetch:
        for item in items_to_fetch:
            expected_path = os.path.join(abs_target_dir, item)
            if not os.path.exists(expected_path):
                print_debug(f"Warning: Expected item '{item}' not found in downloaded {os.path.basename(repo_url)} (branch: {branch_name}).")
    print_debug(f"Successfully fetched branch '{branch_name}' from {repo_url}.")
    return True

def create_patch_file(base_dir, target_dir, patch_file_path, items_to_diff):
    print_debug(f"\nGenerating patch file focusing on {items_to_diff}: {patch_file_path}")
    print("Preparing update (comparing downloaded versions)...")
    parent_temp_dir = os.path.dirname(base_dir)
    filtered_base_name = os.path.basename(base_dir) + "_filtered"
    filtered_target_name = os.path.basename(target_dir) + "_filtered"
    filtered_base_dir = os.path.join(parent_temp_dir, filtered_base_name)
    filtered_target_dir = os.path.join(parent_temp_dir, filtered_target_name)
    try:
        shutil.rmtree(filtered_base_dir, ignore_errors=True)
        shutil.rmtree(filtered_target_dir, ignore_errors=True)
        os.makedirs(filtered_base_dir, exist_ok=True)
        os.makedirs(filtered_target_dir, exist_ok=True)
        print_debug("  Creating temporary filtered directories by copying target items...")
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
                print_debug(f"    Copied '{item}' from base to filtered base.")
            else:
                print_debug(f"    Item '{item}' not found in base directory '{base_dir}', skipping for filtered diff.")
            if os.path.exists(src_target_item):
                if os.path.isdir(src_target_item):
                    shutil.copytree(src_target_item, dest_target_item, dirs_exist_ok=True)
                elif os.path.isfile(src_target_item):
                    os.makedirs(os.path.dirname(dest_target_item), exist_ok=True)
                    shutil.copy2(src_target_item, dest_target_item)
                print_debug(f"    Copied '{item}' from target to filtered target.")
            else:
                print_debug(f"    Item '{item}' not found in target directory '{target_dir}', skipping for filtered diff.")
    except Exception as e:
        print(f"  Error preparing update files during copy.")
        print_debug(f"  Error creating or populating filtered directories: {e}")
        shutil.rmtree(filtered_base_dir, ignore_errors=True)
        shutil.rmtree(filtered_target_dir, ignore_errors=True)
        return False, False
    abs_patch_file_path = os.path.abspath(patch_file_path)
    diff_command = [
        "git",
        "diff",
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
            print_debug(f"  --- Start of generated patch file ({abs_patch_file_path}) ---")
            with open(abs_patch_file_path, 'r', encoding='utf-8', errors='ignore') as pf:
                for i, line in enumerate(pf):
                    if i >= 20:
                        print_debug("    ...")
                        break
                    print_debug(f"    PATCH_LINE: {line.rstrip()}")
            print_debug("  --- End of generated patch file snippet ---")
        except Exception as e_read:
            print_debug(f"  Warning: Could not read patch file for inspection: {e_read}")
    elif DEBUG:
        print_debug(f"  Patch file {abs_patch_file_path} not found after diff command (Exit Code: {diff_exit_code}).")
    patch_exists_and_has_content = os.path.exists(abs_patch_file_path) and os.path.getsize(abs_patch_file_path) > 0
    if diff_exit_code == 0:
        print("Downloaded versions are identical. No changes needed based on remote repos.")
        if os.path.exists(abs_patch_file_path):
            try: os.remove(abs_patch_file_path)
            except Exception as e_rem: print_debug(f"  Warning: Could not remove empty patch file: {e_rem}")
        return True, False
    elif diff_exit_code == 1 and patch_exists_and_has_content:
        print_debug(f"Successfully created patch file: {abs_patch_file_path}")
        return True, True
    else:
        print(f"Error: File comparison failed (git diff exit code: {diff_exit_code}).")
        if not patch_exists_and_has_content and diff_exit_code == 1:
             print_debug("  Reason: Git reported differences, but the patch file is missing or empty.")
        if os.path.exists(abs_patch_file_path):
            try: os.remove(abs_patch_file_path)
            except Exception as e_rem: print_debug(f"  Warning: Could not remove potentially incomplete patch file: {e_rem}")
        return False, False

def force_overwrite_with_job_manager(source_repo_dir, target_install_dir):
    print(f"Performing direct overwrite with Job Manager files...")
    print_debug(f"  Source: {source_repo_dir}")
    print_debug(f"  Target: {target_install_dir}")
    errors = False
    src_app = os.path.join(source_repo_dir, "app")
    dest_app = os.path.join(target_install_dir, "app")
    src_main = os.path.join(source_repo_dir, "main.py")
    dest_main = os.path.join(target_install_dir, "main.py")
    if os.path.exists(src_app) and os.path.isdir(src_app):
        try:
            print("  Overwriting 'app' directory...")
            if os.path.lexists(dest_app):
                print_debug(f"    Removing existing destination: {dest_app}")
                if os.path.isdir(dest_app) and not os.path.islink(dest_app):
                     shutil.rmtree(dest_app)
                else:
                     os.remove(dest_app)
            print_debug(f"    Copying {src_app} to {dest_app}")
            shutil.copytree(src_app, dest_app, dirs_exist_ok=False)
        except Exception as e:
            print(f"  ERROR overwriting 'app' directory.")
            print_debug(f"    Error details: {e}")
            errors = True
    else:
        print("  Warning: Source 'app' directory not found or invalid in downloaded Job Manager repo. Skipping.")
    if os.path.exists(src_main) and os.path.isfile(src_main):
        try:
            print("  Overwriting 'main.py' file...")
            os.makedirs(os.path.dirname(dest_main), exist_ok=True)
            if os.path.isdir(dest_main):
                print_debug(f"    Removing existing directory: {dest_main}")
                shutil.rmtree(dest_main)
            elif os.path.lexists(dest_main) and not os.path.isfile(dest_main):
                 print_debug(f"    Removing existing non-file item: {dest_main}")
                 os.remove(dest_main)
            print_debug(f"    Copying {src_main} to {dest_main}")
            shutil.copy2(src_main, dest_main)
        except Exception as e:
            print(f"  ERROR overwriting 'main.py' file.")
            print_debug(f"    Error details: {e}")
            errors = True
    else:
        print("  Warning: Source 'main.py' file not found or invalid in downloaded Job Manager repo. Skipping.")
    if errors:
        print("Errors occurred during the overwrite process.")
        return False
    else:
        print("Overwrite completed.")
        return True

def install_hans_experimental(target_install_dir, temp_dir):
    print("\nInstalling Hans' Experimental Mod...")
    local_app_dir = os.path.join(target_install_dir, "app")
    hans_temp_checkout = os.path.join(temp_dir, "hans_experimental")
    hans_app_source = os.path.join(hans_temp_checkout, "app")
    print(f"  Removing existing local 'app' directory: {local_app_dir}")
    try:
        if os.path.lexists(local_app_dir):
            if os.path.isdir(local_app_dir) and not os.path.islink(local_app_dir):
                 shutil.rmtree(local_app_dir)
                 print_debug("    Successfully removed local 'app' directory.")
            else:
                 os.remove(local_app_dir)
                 print_debug("    Successfully removed local 'app' (was file/link).")
        else:
             print_debug("    Local 'app' directory did not exist.")
    except Exception as e:
        print(f"  Error: Failed to remove local 'app' directory.")
        print_debug(f"    Error details: {e}")
        return False
    print(f"  Downloading Hans' Experimental files ({HANS_EXPERIMENTAL_BRANCH} branch)...")
    clone_success = fetch_repo_files(
        HANS_EXPERIMENTAL_REPO_URL,
        HANS_EXPERIMENTAL_BRANCH,
        hans_temp_checkout
    )
    if not clone_success:
        print("  Error: Failed to download Hans' Experimental repository.")
        return False
    if not os.path.isdir(hans_app_source):
        print(f"  Error: The required 'app' folder was not found in the downloaded Hans' Experimental repository ({HANS_EXPERIMENTAL_BRANCH} branch).")
        print_debug(f"  Looked for: {hans_app_source}")
        return False
    print(f"  Copying Hans' Experimental 'app' folder to installation directory...")
    try:
        print_debug(f"    Copying {hans_app_source} to {local_app_dir}")
        shutil.copytree(hans_app_source, local_app_dir)
        print("  Hans' Experimental Mod 'app' directory installed successfully.")
        return True
    except Exception as e:
        print(f"  Error: Failed to copy Hans' Experimental 'app' folder.")
        print_debug(f"    Error details: {e}")
        return False

def create_backup(source_dir, backup_dir_base):
    app_dir = os.path.join(source_dir, "app")
    if not os.path.isdir(app_dir):
        print_debug(f"Backup skipped: Source 'app' directory not found at {app_dir}")
        return None
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"app_folder_backup_{timestamp}"
    backup_path = os.path.join(backup_dir_base, backup_name)
    print(f"Creating backup of 'app' directory...")
    print_debug(f"  Source: {app_dir}")
    print_debug(f"  Destination: {backup_path}")
    try:
        shutil.copytree(app_dir, backup_path, symlinks=True)
        print(f"Backup created successfully: {backup_name}")
        return backup_path
    except Exception as e:
        print(f"Error: Failed to create backup of 'app' directory.")
        print_debug(f"  Error details: {e}")
        return None

def copy_missing_files(source_root, dest_root):
    print("Ensuring all required files are present...")
    print_debug(f"  Source root for missing file check: {source_root}")
    print_debug(f"  Destination root for missing file check: {dest_root}")

    if not os.path.isdir(source_root):
        print_debug(f"  Warning: Source directory '{source_root}' not found for missing file check. Skipping.")
        return True

    if not os.path.isdir(dest_root):
        print(f"  Error: Destination directory '{dest_root}' not found. Cannot copy missing files.")
        return False

    copied_count = 0
    errors = False
    try:
        for subdir, dirs, files in os.walk(source_root):
            if '__pycache__' in dirs:
                dirs.remove('__pycache__')
            if '.git' in dirs:
                 dirs.remove('.git')

            for file in files:
                src_path = os.path.join(subdir, file)
                relative_path = os.path.relpath(src_path, source_root)
                dest_path = os.path.join(dest_root, relative_path)
                dest_dir = os.path.dirname(dest_path)

                if not os.path.exists(dest_path) or os.path.isdir(dest_path):
                    if os.path.isdir(dest_path):
                         print_debug(f"  Warning: Destination path '{relative_path}' exists as a directory. Overwriting with file is unsafe, skipping copy.")
                         continue

                    copied_count += 1
                    print_debug(f"  Copying missing file: {relative_path}")
                    try:
                        os.makedirs(dest_dir, exist_ok=True)
                        shutil.copy2(src_path, dest_path)
                    except Exception as e:
                        print(f"  ERROR copying missing file '{relative_path}': {e}")
                        errors = True

        if copied_count > 0:
            print(f"  Copied {copied_count} missing file(s) from the update source.")
        else:
            print_debug("  No missing files needed to be copied.")

    except Exception as e:
        print(f"  An unexpected error occurred during missing file check: {e}")
        if DEBUG:
            traceback.print_exc()
        errors = True

    return not errors


def main():
    print("--- VisoMaster Job Manager Updater (v2) ---")
    script_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in locals() else os.getcwd()
    final_exit_code = 1
    hans_mod_installed_or_requested = False
    backup_created_path = None
    temp_dir_path = None

    local_app_dir_check = os.path.join(script_dir, "app")
    if not os.path.isdir(local_app_dir_check):
        print("\nError: The 'app' directory was not found in this location.")
        print("Please place this script inside your main VisoMaster installation")
        print("directory (the one containing the 'app' folder) and run it again.")
        return 1

    print(f"\nFound 'app' directory in: {script_dir}")
    backup_created_path = create_backup(script_dir, script_dir)
    if backup_created_path:
        backup_display_name = os.path.basename(backup_created_path)
        print(f"  If anything goes wrong, you can restore your files from the backup: {backup_display_name}")
    else:
        print("  Warning: Could not create a backup. Proceeding without one is risky.")
        while True:
            answer = input("  Continue without a backup? [y/N]: ").lower().strip()
            if answer == 'y': break
            elif answer == 'n' or answer == '':
                print("Operation cancelled.")
                return 1
            else: print("  Please answer 'y' or 'n'.")

    if not shutil.which("git"):
        print("\nError: 'git' command not found.")
        print("Git is required for this updater to download necessary files.")
        print("Please install Git and ensure it's in your system's PATH.")
        print_debug("Install from https://git-scm.com/downloads")
        return 1
    print_debug("\nFound Git installation.")

    print("\n--- Hans' Experimental Mod Check ---")
    while True:
        answer = input("Do you currently have Hans' Experimental Mod installed? [y/N]: ").lower().strip()
        if answer == 'y':
            hans_mod_installed_or_requested = True
            break
        elif answer == 'n' or answer == '':
            while True:
                answer_install = input("Would you like to install Hans' Experimental Mod now? [y/N]: ").lower().strip()
                if answer_install == 'y':
                    hans_mod_installed_or_requested = True
                    break
                elif answer_install == 'n' or answer_install == '':
                    hans_mod_installed_or_requested = False
                    break
                else: print("  Please answer 'y' or 'n'.")
            break
        else: print("  Please answer 'y' or 'n'.")

    if hans_mod_installed_or_requested:
        print("\nDisclaimer: Hans' Experimental Mod and the Job Manager are developed")
        print("independently. While this script attempts to install both, compatibility")
        print("is not guaranteed, and you may encounter bugs.")
        while True:
            answer_proceed = input("Are you sure you want to proceed with installing/updating both? [y/N]: ").lower().strip()
            if answer_proceed == 'y':
                print("Proceeding with Hans' Experimental Mod installation/update...")
                break
            elif answer_proceed == 'n' or answer_proceed == '':
                print("Skipping Hans' Experimental Mod installation/update.")
                hans_mod_installed_or_requested = False
                break
            else: print("  Please answer 'y' or 'n'.")


    try:
        temp_parent_dir = script_dir if os.access(script_dir, os.W_OK) else None
        temp_dir_path = tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=temp_parent_dir)
        print_debug(f"\nUsing temporary directory: {temp_dir_path}")

        if hans_mod_installed_or_requested:
            if not install_hans_experimental(script_dir, temp_dir_path):
                print("\nError installing Hans' Experimental Mod. Cannot proceed with Job Manager update.")
                print(f"Your original 'app' folder might be missing or incomplete.")
                if backup_created_path:
                    print(f"Please restore it manually from the backup: {os.path.basename(backup_created_path)}")
                raise Exception("Hans' Mod installation failed.")
            print_debug("Hans' mod installation step completed successfully.")

        print("\n--- Job Manager Update ---")
        job_manager_temp_base = os.path.join(temp_dir_path, "job-manager")
        stock_temp_base = os.path.join(temp_dir_path, "stock")
        patch_file_full_path = os.path.join(temp_dir_path, PATCH_FILENAME)

        if not fetch_repo_files(JOB_MANAGER_REPO_URL, JOB_MANAGER_BRANCH, job_manager_temp_base, TARGET_ITEMS):
            raise Exception("Failed to download Job Manager repository.")
        if not fetch_repo_files(STOCK_REPO_URL, STOCK_BRANCH, stock_temp_base, TARGET_ITEMS):
            raise Exception("Failed to download Stock VisoMaster repository.")
        print("Downloads complete.")

        patch_op_success, patch_generated = create_patch_file(
            stock_temp_base, job_manager_temp_base, patch_file_full_path, TARGET_ITEMS
        )
        if not patch_op_success:
            raise Exception("Patch creation failed due to errors copying files or running diff.")

        if not patch_generated:
            if hans_mod_installed_or_requested:
                print("\nJob Manager changes seem identical to stock VisoMaster for core files.")
                print("Assuming current state (with Hans' Mod) is desired.")
                print("No patch needed or applied for Job Manager.")
            else:
                print("\nYour installation's core files already match the Job Manager version (compared to stock).")
                print("No update needed for the Job Manager.")
            final_exit_code = 0
        else:
            print("Patch file created. Attempting to apply update...")
            abs_patch_file_path = os.path.abspath(patch_file_full_path)

            if DEBUG:
                print(f"\n--- Inspecting Patch File Header: {abs_patch_file_path} ---")
                try:
                    with open(abs_patch_file_path, 'r', encoding='utf-8', errors='ignore') as pf_inspect:
                        for i, line in enumerate(pf_inspect):
                             if i >= 25: break
                             print(f"PATCH_LINE: {repr(line)}")
                except Exception as e_inspect:
                     print(f"ERROR: Could not read patch file for inspection: {e_inspect}")
                print("--- End Patch File Inspection ---")

            apply_base_command = [
                "git", "apply",
                "--ignore-space-change",
                "--ignore-whitespace",
                "-p2",
                abs_patch_file_path
            ]
            apply_check_command = apply_base_command[:2] + ["--check"] + apply_base_command[2:]

            print_debug(f"  Running apply check: {' '.join(quote_path(p) for p in apply_check_command)}")
            check_success, check_exit_code = run_command(apply_check_command, cwd=script_dir, suppress_output=not DEBUG, check=False)

            if check_success and check_exit_code == 0:
                print("  Patch compatibility check successful. Applying...")
                apply_command = apply_base_command
                print_debug(f"  Running apply: {' '.join(quote_path(p) for p in apply_command)}")
                apply_success, apply_exit_code = run_command(apply_command, cwd=script_dir, suppress_output=not DEBUG, check=False)

                if apply_success and apply_exit_code == 0:
                    print("Update applied successfully via patch.")
                    jm_app_src = os.path.join(job_manager_temp_base, "app")
                    local_app_dest = os.path.join(script_dir, "app")
                    jm_main_py_src = os.path.join(job_manager_temp_base, "main.py")
                    local_main_py_dest = os.path.join(script_dir, "main.py")

                    copy_success = copy_missing_files(jm_app_src, local_app_dest)

                    main_py_success = True
                    if os.path.isfile(jm_main_py_src):
                        try:
                            print_debug(f"  Ensuring '{os.path.basename(local_main_py_dest)}' is the Job Manager version...")
                            shutil.copy2(jm_main_py_src, local_main_py_dest)
                        except Exception as e:
                             print(f"  Warning: Could not ensure '{os.path.basename(local_main_py_dest)}' update: {e}")
                             main_py_success = False
                    else:
                         print_debug(f"  Source '{os.path.basename(jm_main_py_src)}' not found in Job Manager repo, skipping explicit copy.")

                    if copy_success and main_py_success:
                        final_exit_code = 0
                    else:
                        print("  Errors occurred during final file synchronization after patch. Update may be incomplete.")
                        final_exit_code = 1
                else:
                    print(f"  Error: Patch application failed (exit code {apply_exit_code}).")
                    print_debug("  Patch application failed even after successful check.")
                    final_exit_code = 1
            else:
                print("  Patch compatibility check failed. Automatic patching not possible.")
                print_debug(f"  Apply check failed (exit code {check_exit_code}). Conflicts likely exist.")
                final_exit_code = 1

            if final_exit_code != 0:
                print("\n--- Fallback: Direct Overwrite ---")
                if hans_mod_installed_or_requested:
                    print("Automatic patching failed. This likely happened because Job Manager")
                    print("changes conflict with the installed Hans' Experimental Mod.")
                    print("\nTo apply the Job Manager update, Hans' Experimental Mod needs to be replaced.")
                    while True:
                        answer_overwrite = input("Overwrite 'app' directory with Job Manager version (replacing Hans' Mod)? [y/N]: ").lower().strip()
                        if answer_overwrite == 'y':
                            if force_overwrite_with_job_manager(job_manager_temp_base, script_dir):
                                print("Job Manager installed via overwrite.")
                                jm_app_src = os.path.join(job_manager_temp_base, "app")
                                local_app_dest = os.path.join(script_dir, "app")
                                if copy_missing_files(jm_app_src, local_app_dest):
                                     final_exit_code = 0
                                else:
                                     print("  Errors occurred during final file check after overwrite. Update may be incomplete.")
                                     final_exit_code = 1
                            else:
                                print("Job Manager installation via overwrite failed.")
                                final_exit_code = 1
                            break
                        elif answer_overwrite == 'n' or answer_overwrite == '':
                            print("Job Manager update cancelled to preserve Hans' Mod.")
                            print("Your files remain in the state before the overwrite attempt.")
                            if backup_created_path:
                                 print(f"If issues persist, restore the backup: {os.path.basename(backup_created_path)}")
                            final_exit_code = 1
                            break
                        else:
                            print("  Please answer 'y' or 'n'.")
                else:
                    print("Automatic patching failed. Overwriting affected files ('app', 'main.py')")
                    print("with the latest Job Manager versions to ensure functionality.")
                    print("WARNING: Any manual changes you made to these files will be lost.")
                    if force_overwrite_with_job_manager(job_manager_temp_base, script_dir):
                        print("Job Manager installed via overwrite.")
                        jm_app_src = os.path.join(job_manager_temp_base, "app")
                        local_app_dest = os.path.join(script_dir, "app")
                        if copy_missing_files(jm_app_src, local_app_dest):
                             final_exit_code = 0
                        else:
                             print("  Errors occurred during final file check after overwrite. Update may be incomplete.")
                             final_exit_code = 1
                    else:
                        print("Job Manager installation via overwrite failed.")
                        final_exit_code = 1

    except Exception as e:
        print("\n--- An Unexpected Error Occurred During Update --- ")
        print_debug(f"Error details: {e}")
        if DEBUG:
            traceback.print_exc()
        final_exit_code = 1
        print("The update process encountered a critical error.")
        if backup_created_path:
            print(f"Please consider restoring your backup: {os.path.basename(backup_created_path)}")

    finally:
        print("\n--- Cleanup ---")
        if temp_dir_path and os.path.exists(temp_dir_path):
            try:
                print(f"Removing temporary directory: {os.path.basename(temp_dir_path)}")
                print_debug(f"  Removing: {temp_dir_path}")
                shutil.rmtree(temp_dir_path, ignore_errors=True)
            except Exception as e_clean:
                 print(f"Warning: Unexpected error during cleanup process for {temp_dir_path}")
                 print_debug(f"  Cleanup error details: {e_clean}")
        else:
            print_debug("No temporary directory path found or directory already removed.")

        print("\n--- Finished ---")
        if final_exit_code == 0:
            print("Update process completed successfully!")
        else:
            print("Update process finished with errors or was cancelled.")
            if backup_created_path and os.path.exists(backup_created_path):
                 print(f"Remember the backup created at: {os.path.basename(backup_created_path)}")
        print("\nThank you for using the updater!")
        print("If you find these tools useful, please consider starring the repositories on GitHub:")
        print(f" - Job Manager: {JOB_MANAGER_REPO_URL}")
        print(f" - VisoMaster: {STOCK_REPO_URL}")
        if hans_mod_installed_or_requested:
            print(f" - Hans' Experimental: {HANS_EXPERIMENTAL_REPO_URL}")

    return final_exit_code

if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = main()
    except Exception as e:
        print("\n--- A Critical Unexpected Error Occurred --- ")
        print("The script had to stop.")
        print_debug(f"Error details: {e}")
        if DEBUG:
            traceback.print_exc()
        exit_code = 1
    finally:
        print("\nPress Enter to exit.")
        input()
        sys.exit(exit_code)
