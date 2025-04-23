import os
import sys
import subprocess
import tempfile
import shutil
import hashlib
import filecmp
import traceback

DEBUG = False
JOB_MANAGER_REPO_URL = "https://github.com/PronPan/VisoMaster-Job-Manager.git"
STOCK_REPO_URL = "https://github.com/visomaster/VisoMaster.git"
JOB_MANAGER_BRANCH = "dev"
STOCK_BRANCH = "main"
TARGET_ITEMS = ["app", "main.py"]
TEMP_PREFIX = "job_manager_patch_"
PATCH_FILENAME = "job-manager_vs_stock.patch"

def print_debug(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)

def get_file_hash(filepath):
    h = hashlib.sha256()
    try:
        with open(filepath, 'rb') as file:
            while True:
                chunk = file.read(4096)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        print_debug(f"    Error calculating hash for {filepath}: {e}")
        return None

def quote_path(path):
    return f'"{path}"' if ' ' in path else path

def run_command(command, cwd=None, suppress_output=False, check=True):
    command_str = ' '.join(command)
    print_debug(f"  Running: {command_str} {'(in '+cwd+')' if cwd else ''}")
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
            is_progress = result.stderr.strip().startswith(('Receiving objects', 'Resolving deltas', 'Updating files'))
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

def fetch_repo_files(repo_url, branch_name, target_dir, items_to_fetch):
    print_debug(f"\nFetching branch '{branch_name}' from {repo_url} (will filter diff later)...")
    print("Downloading necessary files...")
    os.makedirs(target_dir, exist_ok=True)
    clone_command = [
        "git", "clone", "-b", branch_name, "--depth", "1", repo_url, target_dir
    ]
    clone_success, clone_exit_code = run_command(clone_command, suppress_output=not DEBUG)
    if not clone_success:
        print(f"Error: Failed to download required files.")
        print_debug(f"Error: Failed to clone branch '{branch_name}' from repository {repo_url}.")
        if clone_exit_code == 128:
             print_debug(f"  Hint: Branch '{branch_name}' might not exist in {repo_url}.")
        return False
    all_items_present = True
    for item in items_to_fetch:
        expected_path = os.path.join(target_dir, item)
        if not os.path.exists(expected_path):
            print_debug(f"Warning: Expected item '{item}' not found in downloaded files (branch: {branch_name}). It will be ignored.")
    print_debug(f"Successfully fetched branch '{branch_name}' from {repo_url}.")
    return True

def create_patch_file(base_dir, target_dir, patch_file_path, items_to_diff):
    print_debug(f"\nGenerating patch file focusing on {items_to_diff}: {patch_file_path}")
    print("Preparing update...")
    parent_temp_dir = os.path.dirname(base_dir)
    print_debug("  Creating temporary filtered directories by copying target items...")
    filtered_base_name = os.path.basename(base_dir) + "_filtered"
    filtered_target_name = os.path.basename(target_dir) + "_filtered"
    filtered_base_dir = os.path.join(parent_temp_dir, filtered_base_name)
    filtered_target_dir = os.path.join(parent_temp_dir, filtered_target_name)
    try:
        os.makedirs(filtered_base_dir, exist_ok=True)
        os.makedirs(filtered_target_dir, exist_ok=True)
        for item in items_to_diff:
            src_base_item = os.path.join(base_dir, item)
            src_target_item = os.path.join(target_dir, item)
            dest_base_item = os.path.join(filtered_base_dir, item)
            dest_target_item = os.path.join(filtered_target_dir, item)
            if os.path.exists(src_base_item):
                if os.path.isdir(src_base_item):
                    print_debug(f"    Copying {src_base_item} to {filtered_base_dir}")
                    shutil.copytree(src_base_item, dest_base_item, dirs_exist_ok=True)
                elif os.path.isfile(src_base_item):
                    print_debug(f"    Copying {src_base_item} to {filtered_base_dir}")
                    shutil.copy2(src_base_item, dest_base_item)
            else:
                print_debug(f"    Item '{item}' not found in base directory '{base_dir}', skipping for filtered diff.")
            if os.path.exists(src_target_item):
                if os.path.isdir(src_target_item):
                    print_debug(f"    Copying {src_target_item} to {filtered_target_dir}")
                    shutil.copytree(src_target_item, dest_target_item, dirs_exist_ok=True)
                elif os.path.isfile(src_target_item):
                    print_debug(f"    Copying {src_target_item} to {filtered_target_dir}")
                    shutil.copy2(src_target_item, dest_target_item)
            else:
                print_debug(f"    Item '{item}' not found in target directory '{target_dir}', skipping for filtered diff.")
    except Exception as e:
        print(f"  Error preparing update files.")
        print_debug(f"  Error creating or populating filtered directories: {e}")
        shutil.rmtree(filtered_base_dir, ignore_errors=True)
        shutil.rmtree(filtered_target_dir, ignore_errors=True)
        return False, False
    diff_command = [
        "git",
        "diff",
        "--no-index",
        "--binary",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        f"--output={os.path.abspath(patch_file_path)}",
        filtered_base_name,
        filtered_target_name
    ]
    diff_success, diff_exit_code = run_command(diff_command, cwd=parent_temp_dir, suppress_output=not DEBUG, check=False)
    if DEBUG and os.path.exists(patch_file_path):
        try:
            print_debug(f"  --- Start of generated patch file ({patch_file_path}) ---")
            with open(patch_file_path, 'r', encoding='utf-8', errors='ignore') as pf:
                for i, line in enumerate(pf):
                    if i >= 50:
                        break
                    print_debug(f"    PATCH_LINE: {line.rstrip()}")
            print_debug("  --- End of generated patch file snippet ---")
        except Exception as e_read:
            print_debug(f"  Warning: Could not read patch file for inspection: {e_read}")
    elif DEBUG:
        print_debug(f"  Patch file {patch_file_path} not found after diff command.")
    try:
        print_debug("  Cleaning up temporary filtered directories...")
        shutil.rmtree(filtered_base_dir, ignore_errors=True)
        shutil.rmtree(filtered_target_dir, ignore_errors=True)
    except Exception as e_clean:
         print_debug(f"  Warning: Error during cleanup of filtered directories: {e_clean}")
    if not diff_success and diff_exit_code == -1:
        print("Error: Failed to compare downloaded files.")
        print_debug("Error: Failed to execute 'git diff' command.")
        return False, False
    if diff_exit_code > 1:
        print(f"Error: File comparison failed.")
        print_debug(f"Error: 'git diff' command failed with exit code {diff_exit_code}.")
        if os.path.exists(patch_file_path):
            try: os.remove(patch_file_path)
            except Exception as e_rem: print_debug(f"  Warning: Could not remove potentially incomplete patch file: {e_rem}")
        return False, False
    patch_exists_and_has_content = os.path.exists(patch_file_path) and os.path.getsize(patch_file_path) > 0
    if diff_exit_code == 0:
        print("Your installation is already up-to-date. No changes needed.")
        if os.path.exists(patch_file_path):
            try: os.remove(patch_file_path)
            except Exception as e_rem: print_debug(f"  Warning: Could not remove empty patch file: {e_rem}")
        return True, False
    if diff_exit_code == 1 and not patch_exists_and_has_content:
        print("Warning: Differences were found, but the update preparation failed.")
        print_debug("Warning: 'git diff' reported differences, but the patch file is missing or empty.")
        return False, False
    print_debug(f"Successfully created patch file: {patch_file_path}")
    return True, True

def compare_and_copy(src_base_dir, dest_base_dir, items_to_compare):
    changes_made = False
    errors_occurred = False
    abs_src_base = os.path.abspath(src_base_dir)
    abs_dest_base = os.path.abspath(dest_base_dir)
    print(f"\nComparing updated files with your local installation.")
    print_debug(f"\nComparing Job-Manager items {items_to_compare} from '{abs_src_base}' with your local version in '{abs_dest_base}'")
    print("This will overwrite local files if you choose 'y'.")
    for item_name in items_to_compare:
        src_item_path = os.path.join(src_base_dir, item_name)
        dest_item_path = os.path.join(dest_base_dir, item_name)
        if not os.path.exists(src_item_path):
            print_debug(f"  Warning: Source item '{src_item_path}' not found in downloaded Job-Manager version. Skipping comparison for this item.")
            continue
        if os.path.isdir(src_item_path):
            print_debug(f"  Comparing directory: {item_name}")
            if not os.path.exists(dest_item_path):
                 print(f"  Directory missing locally: {dest_item_path}")
                 answer = input(f"    Create local directory '{item_name}' based on the update? [y/N]: ").lower().strip()
                 if answer == 'y':
                     try:
                         print(f"    Copying directory {item_name}...")
                         print_debug(f"    Copying directory {src_item_path} --> {dest_item_path}")
                         shutil.copytree(src_item_path, dest_item_path, dirs_exist_ok=True)
                         changes_made = True
                     except Exception as e:
                         print(f"    Error creating directory {item_name}.")
                         print_debug(f"    Error copying directory {src_item_path} to {dest_item_path}: {e}")
                         errors_occurred = True
                 else:
                     print(f"    Skipping creation of directory: {item_name}")
                 continue
            elif not os.path.isdir(dest_item_path):
                 print(f"  Error: Expected directory but found file locally: {dest_item_path}. Skipping.")
                 errors_occurred = True
                 continue
            for src_root, dirs, files in os.walk(src_item_path):
                relative_path = os.path.relpath(src_root, src_item_path)
                dest_root = os.path.join(dest_item_path, relative_path) if relative_path != '.' else dest_item_path
                for dir_name in list(dirs):
                    src_subdir = os.path.join(src_root, dir_name)
                    dest_subdir = os.path.join(dest_root, dir_name)
                    if not os.path.exists(dest_subdir):
                        try:
                            print_debug(f"    Creating missing subdirectory: {dest_subdir}")
                            os.makedirs(dest_subdir)
                            changes_made = True
                        except Exception as e:
                            print(f"    Error creating subdirectory {os.path.join(item_name, relative_path, dir_name)}.")
                            print_debug(f"    Error creating subdirectory {dest_subdir}: {e}")
                            errors_occurred = True
                    elif not os.path.isdir(dest_subdir):
                        print(f"    Error: Expected directory but found file: {dest_subdir}. Skipping contents.")
                        errors_occurred = True
                        dirs.remove(dir_name)
                for file_name in files:
                    src_file = os.path.join(src_root, file_name)
                    dest_file = os.path.join(dest_root, file_name)
                    relative_file_path = os.path.join(item_name, relative_path, file_name) if relative_path != '.' else os.path.join(item_name, file_name)
                    should_copy = False
                    prompt_needed = False
                    if not os.path.exists(dest_file):
                        print(f"    File missing locally: {dest_file}")
                        answer = input(f"      Copy updated file '{relative_file_path}' to local directory? [y/N]: ").lower().strip()
                        if answer == 'y':
                            should_copy = True
                        else:
                            print(f"      Skipping copy of missing file: {relative_file_path}")
                            should_copy = False
                    elif os.path.isdir(dest_file):
                         print(f"    Error: Expected file but found directory: {dest_file}. Skipping.")
                         errors_occurred = True
                    else:
                        try:
                            if DEBUG:
                                src_hash = get_file_hash(src_file)
                                dest_hash = get_file_hash(dest_file)
                                print_debug(f"      Comparing Hashes: src={src_hash}, dest={dest_hash}")
                                if src_hash and dest_hash and src_hash == dest_hash:
                                     continue
                            if not filecmp.cmp(src_file, dest_file, shallow=False):
                                print(f"    File differs: {dest_file}")
                                prompt_needed = True
                            elif DEBUG:
                                print_debug(f"      Files are identical: {dest_file}")
                        except Exception as e:
                            print(f"    Error comparing file: {relative_file_path}")
                            print_debug(f"    Error comparing {src_file} and {dest_file}: {e}")
                            errors_occurred = True
                            continue
                    if prompt_needed:
                         while True:
                             answer = input(f"      Overwrite local file '{relative_file_path}' with the updated version? [y/N]: ").lower().strip()
                             if answer == 'y':
                                 should_copy = True
                                 break
                             elif answer == 'n' or answer == '':
                                 print(f"      Skipping overwrite for: {relative_file_path}")
                                 should_copy = False
                                 break
                             else:
                                 print("      Please answer 'y' or 'n'.")
                    if should_copy:
                        try:
                            print(f"      Copying updated file: {relative_file_path}")
                            print_debug(f"      Copying {src_file} --> {dest_file}")
                            os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                            shutil.copy2(src_file, dest_file)
                            changes_made = True
                        except Exception as e:
                            print(f"      Error copying file: {relative_file_path}")
                            print_debug(f"      Error copying {src_file} to {dest_file}: {e}")
                            errors_occurred = True
        elif os.path.isfile(src_item_path):
            print_debug(f"  Comparing file: {item_name}")
            should_copy = False
            prompt_needed = False
            if not os.path.exists(dest_item_path):
                 print(f"  File missing locally: {dest_item_path}")
                 answer = input(f"    Copy updated file '{item_name}' to local directory? [y/N]: ").lower().strip()
                 if answer == 'y':
                     should_copy = True
                 else:
                     print(f"    Skipping copy of missing file: {item_name}")
                     should_copy = False
            elif os.path.isdir(dest_item_path):
                 print(f"  Error: Expected file but found directory locally: {dest_item_path}. Skipping.")
                 errors_occurred = True
            else:
                try:
                    if DEBUG:
                        src_hash = get_file_hash(src_item_path)
                        dest_hash = get_file_hash(dest_item_path)
                        print_debug(f"    Comparing Hashes: src={src_hash}, dest={dest_hash}")
                        if src_hash and dest_hash and src_hash == dest_hash:
                             continue
                    if not filecmp.cmp(src_item_path, dest_item_path, shallow=False):
                        print(f"  File differs: {dest_item_path}")
                        prompt_needed = True
                    elif DEBUG:
                        print_debug(f"    Files are identical: {dest_item_path}")
                except Exception as e:
                    print(f"  Error comparing file: {item_name}")
                    print_debug(f"  Error comparing {src_item_path} and {dest_item_path}: {e}")
                    errors_occurred = True
                    continue
            if prompt_needed:
                 while True:
                     answer = input(f"    Overwrite local file '{item_name}' with the updated version? [y/N]: ").lower().strip()
                     if answer == 'y':
                         should_copy = True
                         break
                     elif answer == 'n' or answer == '':
                         print(f"    Skipping overwrite for: {item_name}")
                         should_copy = False
                         break
                     else:
                         print("    Please answer 'y' or 'n'.")
            if should_copy:
                try:
                    print(f"    Copying updated file: {item_name}")
                    print_debug(f"    Copying {src_item_path} --> {dest_item_path}")
                    shutil.copy2(src_item_path, dest_item_path)
                    changes_made = True
                except Exception as e:
                    print(f"    Error copying file: {item_name}")
                    print_debug(f"    Error copying {src_item_path} to {dest_item_path}: {e}")
                    errors_occurred = True
        else:
             print_debug(f"  Warning: Source item '{src_item_path}' is neither a file nor a directory. Skipping.")
    return changes_made, errors_occurred

def main():
    print("--- VisoMaster Job Manager Updater ---")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    patch_file_full_path = os.path.join(script_dir, PATCH_FILENAME)
    final_exit_code = 1
    print_debug(f"\nChecking for required local items in: {script_dir}")
    all_local_items_found = True
    for item in TARGET_ITEMS:
        local_item_path = os.path.join(script_dir, item)
        if not os.path.exists(local_item_path):
             print(f"  Error: Cannot find required local item: '{item}'")
             all_local_items_found = False
        else:
             print_debug(f"  Found: '{item}'")
    if not all_local_items_found:
        print(f"\nError: One or more required items not found.")
        print("Please run this script from the main VisoMaster directory")
        print("which should contain 'app' and 'main.py'.")
        return final_exit_code
    print_debug("All required local items found.")
    if not shutil.which("git"):
        print("\nError: 'git' program not found.")
        print("Git is required for this updater to work.")
        print("Please install Git and ensure it can be run from the command line.")
        print_debug("Install from https://git-scm.com/downloads")
        return final_exit_code
    print_debug("\nFound Git installation.")
    print_debug(f"\nUsing Job Manager branch: '{JOB_MANAGER_BRANCH}' (defined in script)")
    print_debug(f"Using Stock VisoMaster branch: '{STOCK_BRANCH}' (defined in script)")
    print_debug(f"Will manage items: {TARGET_ITEMS}")
    print(f"\nThis script will check for updates for the Job Manager features.")
    print_debug(f"It needs to download parts ({TARGET_ITEMS}) of the official VisoMaster repository")
    print_debug(f"(branch: {STOCK_BRANCH}) and the VisoMaster-Job-Manager repository (branch: {JOB_MANAGER_BRANCH})")
    print_debug(f"to create an update patch or perform an overwrite.")
    while True:
        answer = input("Proceed with checking for updates? [y/N]: ").lower().strip()
        if answer == 'y': break
        elif answer == 'n' or answer == '':
            print("Update check cancelled.")
            return 0
        else: print("Please answer 'y' or 'n'.")
    try:
        temp_parent_dir = script_dir if os.access(script_dir, os.W_OK) else None
        with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX, dir=temp_parent_dir) as tmpdir:
            print_debug(f"\nCreating temporary directory: {tmpdir}")
            job_manager_temp_base = os.path.join(tmpdir, "job-manager")
            stock_temp_base = os.path.join(tmpdir, "stock")
            if not fetch_repo_files(JOB_MANAGER_REPO_URL, JOB_MANAGER_BRANCH, job_manager_temp_base, TARGET_ITEMS):
                return final_exit_code
            if not fetch_repo_files(STOCK_REPO_URL, STOCK_BRANCH, stock_temp_base, TARGET_ITEMS):
                return final_exit_code
            print("Downloads complete.")
            print("Comparing files to prepare update...")
            patch_op_success, patch_generated = create_patch_file(
                stock_temp_base, job_manager_temp_base, patch_file_full_path, TARGET_ITEMS
            )
            if not patch_op_success:
                 print("Update process failed during file comparison.")
                 return final_exit_code
            if not patch_generated:
                final_exit_code = 0
                if os.path.exists(patch_file_full_path):
                    try:
                        os.remove(patch_file_full_path)
                        print_debug(f"Removed empty patch file: {patch_file_full_path}")
                    except Exception as e_rem:
                        print_debug(f"Warning: Could not remove empty patch file: {e_rem}")
                return final_exit_code

            print("Update prepared. Checking compatibility...")
            print_debug(f"\nAttempting to apply patch automatically using 3-way merge: {patch_file_full_path}")

            apply_check_command = ["git", "apply", "--3way", "-p2", "--check", "--allow-empty", patch_file_full_path]
            print_debug(f"  Running 3-way check: {' '.join(apply_check_command)}")
            check_success, check_exit_code = run_command(apply_check_command, cwd=script_dir, suppress_output=not DEBUG, check=False)

            can_apply_cleanly = (check_success and check_exit_code == 0)

            if can_apply_cleanly:
                print("Compatibility check successful (3-way merge possible). Applying update...")
                apply_command = ["git", "apply", "--3way", "-p2", "--allow-empty", patch_file_full_path]
                print_debug(f"  Running 3-way apply: {' '.join(apply_command)}")
                apply_success, apply_exit_code = run_command(apply_command, cwd=script_dir, suppress_output=not DEBUG, check=False)

                if apply_success and apply_exit_code == 0:
                    print("Update applied successfully using 3-way merge.")
                    final_exit_code = 0
                else:
                    print(f"Error: Failed to apply the update automatically using 3-way merge (exit code {apply_exit_code}).")
                    print_debug(f"Error: Failed to apply patch with --3way (exit code {apply_exit_code}). Check for '.rej' (reject) files indicating conflicts.")
                    print("Your local files might be in an inconsistent state.")
                    print("Proceeding to manual overwrite mode might help fix this.")
                    can_apply_cleanly = False
                    final_exit_code = 1

            if not can_apply_cleanly:
                if check_exit_code != 0:
                     print(f"\nAutomatic update failed. Your local files likely have modifications that conflict with the update.")
                     print_debug(f"Automatic update via 3-way patch check failed (exit code {check_exit_code}). This usually means conflicting changes.")
                     print_debug("This can happen if you have a modified version (e.g., Hans' Experimental fork) with overlapping changes.")

                print("\nFALLBACK: Manual Overwrite Mode")
                print("---------------------------------")
                print("Automatic update is NOT possible or failed.")
                print("You can choose to overwrite your local files with the updated versions.")
                print("WARNING: This will discard any custom changes you have made to the affected files.")
                print("You will be asked before each file or directory is overwritten.")
                job_manager_downloaded_base_dir = job_manager_temp_base
                changes_made, errors_occurred = compare_and_copy(job_manager_downloaded_base_dir, script_dir, TARGET_ITEMS)

                print("\n--- Manual Overwrite Summary ---")
                if errors_occurred:
                    print("Some errors occurred during the overwrite process. Please review messages above.")
                    final_exit_code = 1
                elif changes_made:
                    print("Manual overwrite process finished. Files were updated based on your choices.")
                    final_exit_code = 0
                else:
                    print("Manual overwrite process finished. No files needed overwriting (or you chose not to).")
                    final_exit_code = 0

            if final_exit_code == 0 and os.path.exists(patch_file_full_path):
                 try:
                     print_debug(f"\nRemoving used patch file: {patch_file_full_path}")
                     os.remove(patch_file_full_path)
                 except Exception as e_rem:
                     print_debug(f"  Warning: Could not remove patch file: {e_rem}")
            elif final_exit_code != 0 and os.path.exists(patch_file_full_path):
                 print(f"\nNote: Update failed or used fallback. Keeping patch file for review:")
                 print(f"  {patch_file_full_path}")

            print_debug(f"\nTemporary directory {tmpdir} cleaned up.")
    except Exception as e:
        print("\n--- An Unexpected Error Occurred --- ")
        print_debug("--- An Unexpected Error Occurred During Main Processing --- ")
        if DEBUG:
            traceback.print_exc()
        print_debug(f"Error details: {e}")
        final_exit_code = 1
        if 'patch_file_full_path' in locals() and os.path.exists(patch_file_full_path):
             print(f"\nNote: Keeping patch file from potentially incomplete run:")
             print(f"  {patch_file_full_path}")
    return final_exit_code

if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = main()
        if exit_code == 0:
            print("\nUpdate process completed.")
        else:
            print("\nUpdate process finished with errors or required manual intervention.")
    except Exception as e:
        print("\n--- An Unexpected Error Occurred --- ")
        print_debug("--- An Unexpected Error Occurred (Outer Level) --- ")
        if DEBUG:
            traceback.print_exc()
        print_debug(f"Error details: {e}")
        exit_code = 1
    finally:
        print("\nPress Enter to exit.")
        input()
        sys.exit(exit_code)