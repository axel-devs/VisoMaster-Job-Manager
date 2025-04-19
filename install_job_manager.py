import os
import sys
import subprocess
import tempfile
import shutil
import hashlib
import filecmp

JOB_MANAGER_REPO_URL = "https://github.com/PronPan/VisoMaster-Job-Manager.git"
STOCK_REPO_URL = "https://github.com/visomaster/VisoMaster.git"
APP_DIR = "app"
TEMP_PREFIX = "job_manager_patch_"
PATCH_FILENAME = "job-manager_vs_stock.patch"

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
        print(f"    Error calculating hash for {filepath}: {e}")
        return None

def run_command(command, cwd=None, suppress_output=False, check=True):
    command_str = ' '.join(command)
    print(f"  Running: {command_str} {'(in '+cwd+')' if cwd else ''}")
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
        if result.stderr:
            is_progress = result.stderr.strip().startswith(('Receiving objects', 'Resolving deltas', 'Updating files'))
            if not is_progress or exit_code != 0:
                for line in result.stderr.strip().splitlines():
                    print(f"    stderr: {line}")
        if not suppress_output and result.stdout:
            for line in result.stdout.strip().splitlines():
                print(f"    stdout: {line}")
        return True, exit_code
    except FileNotFoundError:
        print(f"  Error: Command not found - '{command[0]}'. Is Git installed and in your system's PATH?")
        return False, -1
    except subprocess.CalledProcessError as e:
        print(f"  Error: Command failed with exit code {e.returncode}")
        if e.stderr:
            for line in e.stderr.strip().splitlines():
                print(f"    stderr: {line}")
        if e.stdout:
            for line in e.stdout.strip().splitlines():
                print(f"    stdout: {line}")
        return False, e.returncode
    except Exception as e:
        print(f"  An unexpected error occurred while running command: {e}")
        import traceback
        traceback.print_exc()
        return False, -1

def fetch_repo_app_dir(repo_url, target_dir):
    print(f"\nFetching '{APP_DIR}' from {repo_url}...")
    clone_command = [
        "git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", repo_url, target_dir
    ]
    sparse_checkout_command = [
        "git", "sparse-checkout", "set", "--cone", APP_DIR
    ]
    sparse_checkout_command_fallback = [
        "git", "sparse-checkout", "set", APP_DIR
    ]
    clone_success, _ = run_command(clone_command, suppress_output=True)
    if not clone_success:
        print(f"Error: Failed to clone repository {repo_url}.")
        return False

    sparse_success, _ = run_command(sparse_checkout_command, cwd=target_dir, suppress_output=True)
    if not sparse_success:
        print("  Sparse checkout with --cone failed, trying fallback...")
        sparse_fallback_success, _ = run_command(sparse_checkout_command_fallback, cwd=target_dir, suppress_output=True)
        if not sparse_fallback_success:
            print(f"Error: Failed to configure sparse checkout for {repo_url}.")
            return False

    expected_app_path = os.path.join(target_dir, APP_DIR)
    if not os.path.isdir(expected_app_path):
        print(f"Error: Failed to checkout the '{APP_DIR}' directory from {repo_url}.")
        print("The repository structure might have changed, or there was a network issue.")
        return False
    print(f"Successfully fetched '{APP_DIR}' from {repo_url}.")
    return True

def create_patch_file(base_dir, target_dir, patch_file_path):
    print(f"\nGenerating patch file: {patch_file_path}")
    parent_dir = os.path.dirname(base_dir)
    rel_base_app = os.path.join(os.path.basename(base_dir), APP_DIR)
    rel_target_app = os.path.join(os.path.basename(target_dir), APP_DIR)
    diff_command = [
        "git", "diff",
        "--no-index",
        "--binary",
        f"--output={os.path.abspath(patch_file_path)}",
        rel_base_app,
        rel_target_app
    ]

    diff_success, diff_exit_code = run_command(diff_command, cwd=parent_dir, suppress_output=True, check=False)

    if not diff_success and diff_exit_code == -1:
        print("Error: Failed to execute 'git diff' command.")
        return False, False

    if diff_exit_code > 1:
        print(f"Error: 'git diff' command failed with exit code {diff_exit_code}.")
        if os.path.exists(patch_file_path):
            try: os.remove(patch_file_path)
            except Exception as e_rem: print(f"  Warning: Could not remove potentially incomplete patch file: {e_rem}")
        return False, False

    patch_exists_and_has_content = os.path.exists(patch_file_path) and os.path.getsize(patch_file_path) > 0

    if diff_exit_code == 0:
        print("No differences found between stock and Job-Manager version. No patch created.")
        if os.path.exists(patch_file_path):
            try: os.remove(patch_file_path)
            except Exception as e_rem: print(f"  Warning: Could not remove empty patch file: {e_rem}")
        return True, False

    if diff_exit_code == 1 and not patch_exists_and_has_content:
        print("Warning: 'git diff' reported differences, but the patch file is missing or empty.")
        return False, False

    print(f"Successfully created patch file: {patch_file_path}")
    return True, True

def compare_and_copy(src_dir, dest_dir):
    changes_made = False
    errors_occurred = False
    abs_src_dir = os.path.abspath(src_dir)
    abs_dest_dir = os.path.abspath(dest_dir)
    print(f"\nComparing '{abs_src_dir}' (Job-Manager version) with '{abs_dest_dir}' (Your version)")
    print("This will overwrite files if you choose 'y'.")

    for src_root, dirs, files in os.walk(src_dir):
        relative_path = os.path.relpath(src_root, src_dir)
        dest_root = os.path.join(dest_dir, relative_path) if relative_path != '.' else dest_dir

        for dir_name in list(dirs):
            dest_subdir = os.path.join(dest_root, dir_name)
            if not os.path.exists(dest_subdir):
                try:
                    print(f"  Creating missing directory: {dest_subdir}")
                    os.makedirs(dest_subdir)
                    changes_made = True
                except Exception as e:
                    print(f"  Error creating directory {dest_subdir}: {e}")
                    errors_occurred = True
            elif not os.path.isdir(dest_subdir):
                print(f"  Error: Expected directory but found file: {dest_subdir}. Skipping contents.")
                errors_occurred = True
                dirs.remove(dir_name)

        for file_name in files:
            src_file = os.path.join(src_root, file_name)
            dest_file = os.path.join(dest_root, file_name)
            should_copy = False
            prompt_needed = False

            if not os.path.exists(dest_file):
                print(f"  File missing locally: {dest_file}")
                should_copy = True
            elif os.path.isdir(dest_file):
                 print(f"  Error: Expected file but found directory: {dest_file}. Skipping.")
                 errors_occurred = True
            else:
                try:
                    src_hash = get_file_hash(src_file)
                    dest_hash = get_file_hash(dest_file)
                    if src_hash and dest_hash and src_hash == dest_hash:
                         continue
                    if not filecmp.cmp(src_file, dest_file, shallow=False):
                        print(f"  File differs: {dest_file}")
                        prompt_needed = True
                except Exception as e:
                    print(f"  Error comparing {src_file} and {dest_file}: {e}")
                    errors_occurred = True
                    continue

            if prompt_needed:
                while True:
                    answer = input("    Overwrite local file with Job-Manager's version? [y/N]: ").lower().strip()
                    if answer == 'y':
                        should_copy = True
                        break
                    elif answer == 'n' or answer == '':
                        print(f"    Skipping overwrite for: {dest_file}")
                        should_copy = False
                        break
                    else:
                        print("    Please answer 'y' or 'n'.")

            if should_copy:
                try:
                    print(f"    Copying {src_file} --> {dest_file}")
                    os.makedirs(os.path.dirname(dest_file), exist_ok=True)
                    shutil.copy2(src_file, dest_file)
                    changes_made = True
                except Exception as e:
                    print(f"    Error copying {src_file} to {dest_file}: {e}")
                    errors_occurred = True

    return changes_made, errors_occurred

def main():
    print("--- Axel's VisoMaster Job Manager Patch ---")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    patch_file_full_path = os.path.join(script_dir, PATCH_FILENAME)
    final_exit_code = 1

    local_app_dir = os.path.join(script_dir, APP_DIR)
    if not os.path.isdir(local_app_dir):
        print(f"\nError: Cannot find the '{APP_DIR}' directory in the script's folder ({script_dir}).")
        print("Please place this script in the root directory of your VisoMaster installation.")
        return final_exit_code
    print(f"\nFound local '{APP_DIR}' directory: {local_app_dir}")

    if not shutil.which("git"):
        print("\nError: 'git' command not found.")
        print("Git is required to download and compare the necessary files.")
        print("Please install Git (e.g., from https://git-scm.com/downloads) and ensure it's in your system's PATH.")
        return final_exit_code
    print("\nFound Git installation.")

    print(f"\nThis script needs to download parts of the official VisoMaster repository")
    print(f"and the VisoMaster-Job-Manager repository to create an update patch.")
    while True:
        answer = input("Do you want to proceed with downloading and update attempt? [y/N]: ").lower().strip()
        if answer == 'y': break
        elif answer == 'n' or answer == '':
            print("Aborted by user.")
            return 0
        else: print("Please answer 'y' or 'n'.")

    try:
        with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX, dir=script_dir) as tmpdir:
            print(f"\nCreating temporary directory: {tmpdir}")
            job_manager_temp_base = os.path.join(tmpdir, "job-manager")
            stock_temp_base = os.path.join(tmpdir, "stock")

            if not fetch_repo_app_dir(JOB_MANAGER_REPO_URL, job_manager_temp_base):
                return final_exit_code

            if not fetch_repo_app_dir(STOCK_REPO_URL, stock_temp_base):
                return final_exit_code
            print("\nDownloads complete.")

            patch_op_success, patch_generated = create_patch_file(
                stock_temp_base, job_manager_temp_base, patch_file_full_path
            )
            if not patch_op_success:
                 print("Update process failed during patch creation.")
                 return final_exit_code
            if not patch_generated:
                final_exit_code = 0
                if os.path.exists(patch_file_full_path):
                    try: os.remove(patch_file_full_path)
                    except Exception: pass
                return final_exit_code

            print(f"\nAttempting to apply patch automatically: {patch_file_full_path}")

            apply_check_command = ["git", "apply", "-p2", "--check", "--allow-empty", "--verbose", patch_file_full_path]
            check_success, check_exit_code = run_command(apply_check_command, cwd=script_dir, suppress_output=False, check=False)
            can_apply_cleanly = (check_success and check_exit_code == 0)

            if can_apply_cleanly:
                print("  Patch check successful. Applying changes...")
                apply_command = ["git", "apply", "-p2", "--allow-empty", patch_file_full_path]
                apply_success, apply_exit_code = run_command(apply_command, cwd=script_dir, suppress_output=False, check=False)
                if apply_success and apply_exit_code == 0:
                    print("Update applied successfully via patch method.")
                    final_exit_code = 0
                else:
                    print(f"Error: Failed to apply patch (exit code {apply_exit_code}) even after successful check. Installation might be inconsistent.")
                    final_exit_code = 1
            else:
                print(f"\nAutomatic update via patch check failed (exit code {check_exit_code}). This usually means conflicting changes.")
                print("This can happen if you have a modified version (e.g., Hans' Experimental fork).")
                print("\nAutomatic patch application is NOT possible, and continuing will REMOVE your custom changes to VisoMaster...")
                print("You will be asked before any local files are overwritten.")
                print("WARNING: Overwriting files will discard your local changes in those files.")
                job_manager_downloaded_app_dir = os.path.join(job_manager_temp_base, APP_DIR)
                changes_made, errors_occurred = compare_and_copy(job_manager_downloaded_app_dir, local_app_dir)
                print("\n--- Fallback Overwrite Summary ---")
                if errors_occurred:
                    print("Some errors occurred during the overwrite process. Please review messages.")
                    final_exit_code = 1
                elif changes_made:
                    print("Overwrite process finished. Some files were added or updated based on your choices.")
                    final_exit_code = 0
                else:
                    print("Overwrite process finished. No files needed overwriting (or you chose not to).")
                    final_exit_code = 0

            if final_exit_code == 0 and os.path.exists(patch_file_full_path):
                 try:
                     print(f"\nRemoving used patch file: {patch_file_full_path}")
                     os.remove(patch_file_full_path)
                 except Exception as e_rem:
                     print(f"  Warning: Could not remove patch file: {e_rem}")
            elif final_exit_code != 0 and os.path.exists(patch_file_full_path):
                 print(f"\nNote: Keeping patch file for review due to errors/fallback: {patch_file_full_path}")

            print(f"\nTemporary directory {tmpdir} cleaned up.")
    except Exception as e:
        print("\n--- An Unexpected Error Occurred During Main Processing --- ")
        import traceback
        traceback.print_exc()
        print(f"Error details: {e}")
        final_exit_code = 1
        if 'patch_file_full_path' in locals() and os.path.exists(patch_file_full_path):
             print(f"Note: Keeping patch file from potentially incomplete run: {patch_file_full_path}")
    return final_exit_code

if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = main()
        if exit_code == 0:
            print("\nUpdate process completed successfully.")
        else:
            print("\nUpdate process finished with errors or required user intervention during fallback.")
    except Exception as e:
        print("\n--- An Unexpected Error Occurred (Outer Level) --- ")
        import traceback
        traceback.print_exc()
        print(f"Error details: {e}")
        exit_code = 1
    finally:
        print("\nPress Enter to exit.")
        input()
        sys.exit(exit_code)