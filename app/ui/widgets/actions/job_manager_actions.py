import json
from pathlib import Path
import uuid
import copy
from functools import partial
from typing import TYPE_CHECKING, Dict
import os
import shutil
from PySide6.QtCore import QThread, Signal
from PySide6 import QtWidgets
import numpy as np
from PySide6.QtWidgets import QInputDialog, QMessageBox
import threading
import re

from app.ui.widgets.actions import common_actions as common_widget_actions
from app.ui.widgets.actions import card_actions
from app.ui.widgets.actions import list_view_actions
from app.ui.widgets.actions import video_control_actions
from app.ui.widgets.actions import layout_actions
from app.ui.widgets import ui_workers
from app.helpers.typing_helper import ParametersTypes, MarkerTypes
import app.helpers.miscellaneous as misc_helpers
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.ui.main_ui import MainWindow


jobs_dir = os.path.join(os.getcwd(), "jobs")
os.makedirs(jobs_dir, exist_ok=True)  # Ensure the directory exists

# Add a global event for job loading
job_loaded_event = threading.Event()

def convert_parameters_to_job_type(main_window: "MainWindow", parameters: dict|ParametersTypes, convert_type: dict|misc_helpers.ParametersDict):
    if convert_type==dict:
        parameters = parameters.data
    elif convert_type==misc_helpers.ParametersDict:
        parameters = misc_helpers.ParametersDict(parameters, main_window.default_parameters)
    return parameters

def convert_markers_to_job_type(main_window: "MainWindow", markers: MarkerTypes, convert_type: dict|misc_helpers.ParametersDict):
    # Convert Parameters inside the markers from ParametersDict to dict
    for _,marker_data in markers.items():
        for target_face_id, target_parameters in marker_data['parameters'].items():
            marker_data['parameters'][target_face_id] = convert_parameters_to_job_type(main_window, target_parameters, convert_type)
    return markers

def save_job(main_window, job_name: str, use_job_name_for_output: bool = True):
    """Saves the current workspace as a job in the 'jobs' directory."""
    data_filename = os.path.join(jobs_dir, f"{job_name}")
    save_job_workspace(main_window, data_filename, use_job_name_for_output)
    print(f"[DEBUG] Job saved: {data_filename}")

def list_jobs():
    """Lists all saved jobs from the 'jobs' directory."""
    if not os.path.exists(jobs_dir):
        return []
    return [f.replace('.json', '') for f in os.listdir(jobs_dir) if f.endswith('.json')]

def delete_job(main_window: "MainWindow"):
    """Deletes the selected job(s) from the 'jobs' directory after confirmation."""
    selected_jobs = get_selected_jobs(main_window)
    if not selected_jobs:
        QtWidgets.QMessageBox.warning(main_window, "No Job Selected", "Please select one or more jobs to delete.")
        return False

    confirm = QtWidgets.QMessageBox.question(
        main_window,
        "Confirm Deletion",
        f"Are you sure you want to delete the selected job{'s' if len(selected_jobs) > 1 else ''}?\n\n" + ", ".join(selected_jobs),
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
    )
    if confirm != QtWidgets.QMessageBox.Yes:
        return False

    deleted_any = False
    for job_name in selected_jobs:
        job_file = os.path.join(jobs_dir, f"{job_name}.json")
        if os.path.exists(job_file):
            os.remove(job_file)
            print(f"[DEBUG] Job deleted: {job_file}")
            deleted_any = True
        else:
            print(f"[DEBUG] Job file not found for deletion: {job_file}")
    if deleted_any:
        refresh_job_list(main_window)
        return True
    else:
        QtWidgets.QMessageBox.warning(main_window, "Job(s) Not Found", "None of the selected jobs exist.")
        return False

def load_job(main_window):
    """Loads whichever job is currently selected in the ListWidget."""
    selected_jobs = get_selected_jobs(main_window)
    if not selected_jobs:
        QMessageBox.warning(main_window, "No Job Selected", "Please select a job from the list.")
        return
    if len(selected_jobs) > 1:
        QMessageBox.warning(main_window, "Multiple Jobs Selected", "You can only load one job at a time. Please select a single job to load.")
        return
    job_name = selected_jobs[0]
    load_job_by_name(main_window, job_name)

def load_job_workspace(main_window: "MainWindow", job_name: str):
    from app.ui.widgets import widget_components
    print("[DEBUG] Loading job workspace...")
    jobs_dir = os.path.join(os.getcwd(), "jobs"); os.makedirs(jobs_dir, exist_ok=True)
    data_filename = os.path.join(jobs_dir, f"{job_name}.json")
    if not Path(data_filename).is_file():
        print(f"[DEBUG] No valid file found for job: {job_name}."); return
    with open(data_filename, 'r') as data_file: data = json.load(data_file)

    # Define steps for progress
    steps = [
        "Target Videos", "Input Faces", "Embeddings", "Target Faces", "Controls", "Swap Faces", "Markers", "Misc Fields", "Finalizing"
    ]
    total_steps = len(steps)
    progress_dialog = widget_components.JobLoadingDialog(total_steps, parent=main_window)
    progress_dialog.show()
    QtWidgets.QApplication.processEvents()
    step_idx = 0

    # --- Clear previous state --- 
    # Clear selected video button reference *before* clearing the list/widgets
    main_window.selected_video_button = None 

    # Clear job name and output flag on main_window for later use
    main_window.current_job_name = job_name
    main_window.use_job_name_for_output = data.get('use_job_name_for_output', False)
    list_view_actions.clear_stop_loading_input_media(main_window)
    list_view_actions.clear_stop_loading_target_media(main_window)
    main_window.target_videos = {}
    card_actions.clear_input_faces(main_window)
    card_actions.clear_target_faces(main_window)
    card_actions.clear_merged_embeddings(main_window)
    if hasattr(main_window, 'selected_video_button'):
        btn = main_window.selected_video_button
        if btn and (not hasattr(btn, 'media_id') or btn.media_id not in main_window.target_videos):
            main_window.selected_video_button = None
    # Step 1: Target Videos
    step_idx += 1
    progress_dialog.update_progress(step_idx, total_steps, steps[step_idx-1])
    target_medias_data = data.get('target_medias_data', [])
    target_medias_files_list, target_media_ids = zip(*[(m['media_path'], m['media_id']) for m in target_medias_data]) if target_medias_data else ([], [])
    main_window.video_loader_worker = ui_workers.TargetMediaLoaderWorker(main_window=main_window, folder_name=False, files_list=target_medias_files_list, media_ids=target_media_ids)
    main_window.video_loader_worker.thumbnail_ready.connect(partial(list_view_actions.add_media_thumbnail_to_target_videos_list, main_window))
    main_window.video_loader_worker.run()
    selected_media_id = data.get('selected_media_id', False)
    if selected_media_id and main_window.target_videos.get(selected_media_id): main_window.target_videos[selected_media_id].click()

    # Step 2: Input Faces
    step_idx += 1
    progress_dialog.update_progress(step_idx, total_steps, steps[step_idx-1])
    input_faces_data = data.get('input_faces_data', {})
    input_media_paths, input_face_ids = zip(*[(f['media_path'], face_id) for face_id, f in input_faces_data.items()]) if input_faces_data else ([], [])
    main_window.input_faces_loader_worker = ui_workers.InputFacesLoaderWorker(main_window=main_window, folder_name=False, files_list=input_media_paths, face_ids=input_face_ids)
    main_window.input_faces_loader_worker.thumbnail_ready.connect(partial(list_view_actions.add_media_thumbnail_to_source_faces_list, main_window))
    main_window.input_faces_loader_worker.finished.connect(partial(common_widget_actions.refresh_frame, main_window))
    main_window.input_faces_loader_worker.files_list = list(main_window.input_faces_loader_worker.files_list)
    main_window.input_faces_loader_worker.run()

    # Step 3: Embeddings
    step_idx += 1
    progress_dialog.update_progress(step_idx, total_steps, steps[step_idx-1])
    for embedding_id, embedding_data in data.get('embeddings_data', {}).items():
        embedding_store = {embed_model: np.array(embed) for embed_model, embed in embedding_data['embedding_store'].items()}
        list_view_actions.create_and_add_embed_button_to_list(main_window, embedding_data['embedding_name'], embedding_store, embedding_id=embedding_id)

    # Step 4: Target Faces
    step_idx += 1
    progress_dialog.update_progress(step_idx, total_steps, steps[step_idx-1])
    for face_id, target_face_data in data.get('target_faces_data', {}).items():
        cropped_face = np.array(target_face_data['cropped_face']).astype('uint8')
        pixmap = common_widget_actions.get_pixmap_from_frame(main_window, cropped_face)
        embedding_store = {embed_model: np.array(embed) for embed_model, embed in target_face_data['embedding_store'].items()}
        list_view_actions.add_media_thumbnail_to_target_faces_list(main_window, cropped_face, embedding_store, pixmap, face_id)
        main_window.parameters[face_id] = convert_parameters_to_job_type(main_window, target_face_data['parameters'], misc_helpers.ParametersDict)
        for assigned_id in target_face_data.get('assigned_merged_embeddings', []):
            if assigned_id in main_window.merged_embeddings: main_window.target_faces[face_id].assigned_merged_embeddings[assigned_id] = main_window.merged_embeddings[assigned_id].embedding_store
        for assigned_id in target_face_data.get('assigned_input_faces', []):
            if assigned_id in main_window.input_faces: main_window.target_faces[face_id].assigned_input_faces[assigned_id] = main_window.input_faces[assigned_id].embedding_store
        main_window.target_faces[face_id].assigned_input_embedding = {embed_model: np.array(embed) for embed_model, embed in target_face_data.get('assigned_input_embedding', {}).items()}

    # Step 5: Controls
    step_idx += 1
    progress_dialog.update_progress(step_idx, total_steps, steps[step_idx-1])
    for control_name, control_value in data.get('control', {}).items():
        main_window.control[control_name] = control_value

    # Step 6: Swap Faces
    step_idx += 1
    progress_dialog.update_progress(step_idx, total_steps, steps[step_idx-1])
    swap_faces_state = data.get('swap_faces_enabled', True)
    main_window.swapfacesButton.setChecked(swap_faces_state)
    if swap_faces_state:
        video_control_actions.process_swap_faces(main_window)
    print(f"[DEBUG] Swap Faces button state restored: {swap_faces_state}")

    # Step 7: Markers
    step_idx += 1
    progress_dialog.update_progress(step_idx, total_steps, steps[step_idx-1])
    video_control_actions.remove_all_markers(main_window)
    data['markers'] = convert_markers_to_job_type(main_window, data.get('markers', {}), misc_helpers.ParametersDict)
    for marker_position, marker_data in data['markers'].items():
        video_control_actions.add_marker(main_window, marker_data['parameters'], marker_data['control'], int(marker_position))

    # Load job start/end frames
    main_window.job_start_frame = data.get('job_start_frame', None)
    main_window.job_end_frame = data.get('job_end_frame', None)

    # Step 8: Misc Fields
    step_idx += 1
    progress_dialog.update_progress(step_idx, total_steps, steps[step_idx-1])
    main_window.last_target_media_folder_path = data.get('last_target_media_folder_path', '')
    main_window.last_input_media_folder_path = data.get('last_input_media_folder_path', '')
    main_window.loaded_embedding_filename = data.get('loaded_embedding_filename', '')
    common_widget_actions.set_control_widgets_values(main_window)
    output_folder = data.get('control', {}).get('OutputMediaFolder', '')
    common_widget_actions.create_control(main_window, 'OutputMediaFolder', output_folder)
    main_window.outputFolderLineEdit.setText(output_folder)
    layout_actions.fit_image_to_view_onchange(main_window)
    common_widget_actions.set_widgets_values_using_face_id_parameters(main_window, face_id=False)
    print(f"[DEBUG] Loaded workspace from: {data_filename}")
    progress_dialog.close()
    # Update slider visuals after loading everything
    main_window.videoSeekSlider.update()
    job_loaded_event.set()

def save_job_workspace(main_window: "MainWindow", job_name: str, use_job_name_for_output: bool = True):
    print("[DEBUG] Saving job workspace...")
    jobs_dir = os.path.join(os.getcwd(), "jobs"); os.makedirs(jobs_dir, exist_ok=True)
    data_filename = os.path.join(jobs_dir, f"{job_name}.json")
    target_faces_data = {}; embeddings_data = {}; input_faces_data = {}
    for face_id, input_face in main_window.input_faces.items():
        input_faces_data[face_id] = {'media_path': input_face.media_path}
    for face_id, target_face in main_window.target_faces.items():
        target_faces_data[face_id] = {
            'cropped_face': target_face.cropped_face.tolist(),
            'embedding_store': {embed_model: embedding.tolist() for embed_model, embedding in target_face.embedding_store.items()},
            'parameters': main_window.parameters[face_id].data.copy(),
            'control': main_window.control.copy(),
            'assigned_input_faces': [input_face_id for input_face_id in target_face.assigned_input_faces.keys()],
            'assigned_merged_embeddings': [embedding_id for embedding_id in target_face.assigned_merged_embeddings.keys()],
            'assigned_input_embedding': {embed_model: embedding.tolist() for embed_model, embedding in target_face.assigned_input_embedding.items()}
        }
    for embedding_id, embed_button in main_window.merged_embeddings.items():
        embeddings_data[embedding_id] = {
            'embedding_store': {embed_model: embedding.tolist() for embed_model, embedding in embed_button.embedding_store.items()},
            'embedding_name': embed_button.embedding_name
        }
    target_medias_data = [{'media_id': media_id, 'media_path': target_media.media_path}  
                          for media_id, target_media in main_window.target_videos.items() if not target_media.is_webcam]
    selected_media_id = main_window.selected_video_button.media_id if main_window.selected_video_button else False
    markers = convert_markers_to_job_type(main_window, copy.deepcopy(main_window.markers), dict)
    swap_faces_state = True  # Always set to True when saving
    print(f"[DEBUG] Swap Faces button state saved: {swap_faces_state}")
    save_data = {
        'selected_media_id': selected_media_id,
        'target_medias_data': target_medias_data,
        'target_faces_data': target_faces_data,
        'embeddings_data': embeddings_data,
        'input_faces_data': input_faces_data,
        'markers': markers,
        'control': main_window.control,
        'last_target_media_folder_path': main_window.last_target_media_folder_path,
        'last_input_media_folder_path': main_window.last_input_media_folder_path,
        'loaded_embedding_filename': main_window.loaded_embedding_filename,
        'current_widget_parameters': convert_parameters_to_job_type(main_window, main_window.current_widget_parameters, dict),
        'swap_faces_enabled': swap_faces_state,
        'use_job_name_for_output': use_job_name_for_output,
        'job_start_frame': main_window.job_start_frame,
        'job_end_frame': main_window.job_end_frame
    }
    with open(data_filename, 'w') as data_file:
        json.dump(save_data, data_file, indent=4)
    print(f"[DEBUG] Job successfully saved to: {data_filename}")

def update_job_manager_buttons(main_window):
    """Enable/disable job manager buttons based on selection and job list state."""
    job_list = main_window.jobQueueList
    selected_count = len(job_list.selectedItems()) if job_list else 0
    job_count = job_list.count() if job_list else 0

    # Enable/disable based on selection
    enable_on_selection = selected_count > 0
    if hasattr(main_window, 'buttonProcessSelected') and main_window.buttonProcessSelected:
        main_window.buttonProcessSelected.setEnabled(enable_on_selection)
    if hasattr(main_window, 'loadJobButton') and main_window.loadJobButton:
        main_window.loadJobButton.setEnabled(enable_on_selection)
    if hasattr(main_window, 'deleteJobButton') and main_window.deleteJobButton:
        main_window.deleteJobButton.setEnabled(enable_on_selection)

    # Enable/disable 'All' based on job list
    if hasattr(main_window, 'buttonProcessAll') and main_window.buttonProcessAll:
        main_window.buttonProcessAll.setEnabled(job_count > 0)

def setup_job_manager_ui(main_window):
    """Initialize UI widgets, connect signals, and refresh the job list for the job manager."""
    main_window.addJobButton = main_window.findChild(QtWidgets.QPushButton, "addJobButton")
    main_window.deleteJobButton = main_window.findChild(QtWidgets.QPushButton, "deleteJobButton")
    main_window.jobQueueList = main_window.findChild(QtWidgets.QListWidget, "jobQueueList")
    main_window.buttonProcessSelected = main_window.findChild(QtWidgets.QPushButton, "buttonProcessSelected")
    main_window.buttonProcessAll = main_window.findChild(QtWidgets.QPushButton, "buttonProcessAll")
    main_window.loadJobButton = main_window.findChild(QtWidgets.QPushButton, "loadJobButton")
    main_window.refreshJobListButton = main_window.findChild(QtWidgets.QPushButton, "refreshJobListButton")

    # Enable multi-selection for the job list
    if main_window.jobQueueList:
        main_window.jobQueueList.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)

    # Connect buttons
    if main_window.buttonProcessAll:
        main_window.buttonProcessAll.clicked.connect(lambda: start_processing_all_jobs(main_window))
    if main_window.buttonProcessSelected:
        main_window.buttonProcessSelected.clicked.connect(lambda: process_selected_job(main_window))
    if main_window.addJobButton and main_window.deleteJobButton:
        connect_job_manager_signals(main_window)
    if main_window.refreshJobListButton:
        main_window.refreshJobListButton.clicked.connect(lambda: refresh_job_list(main_window))
    main_window.jobQueueList.itemSelectionChanged.connect(lambda: update_job_manager_buttons(main_window))
    refresh_job_list(main_window)
    update_job_manager_buttons(main_window)
    main_window.job_processor = None

def prompt_job_name(main_window):
    """Prompt user to enter a job name before saving, with option to set output file name."""
    from app.ui.widgets import widget_components
    # Check for workspace readiness: at least one source/target face or embedding selected
    has_source_face = bool(getattr(main_window, 'input_faces', {})) and any(getattr(main_window, 'input_faces', {}))
    has_target_face = bool(getattr(main_window, 'target_faces', {})) and any(getattr(main_window, 'target_faces', {}))
    has_embedding = bool(getattr(main_window, 'merged_embeddings', {})) and any(getattr(main_window, 'merged_embeddings', {}))

    # Check for at least one target face
    if not has_target_face:
        reply = QMessageBox.warning(main_window, "Confirm Save", 
                                    "No target face selected! No face swaps will happen for this job. Proceed anyway?",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.No:
            return

    # Check OutputMediaFolder is not empty
    output_folder = main_window.control.get('OutputMediaFolder', '').strip() if hasattr(main_window, 'control') else ''
    if not output_folder:
        QMessageBox.warning(main_window, "Workspace Not Ready", "Select an Output Folder. Your workspace must be fully ready to record before saving a job.")
        return

    # Check if ANY target face has input faces or embeddings assigned
    at_least_one_target_has_input = False
    if main_window.target_faces:
        for face_id, target_face in main_window.target_faces.items():
            has_input_faces = bool(getattr(target_face, 'assigned_input_faces', {})) and any(getattr(target_face, 'assigned_input_faces', {}))
            has_merged_embeddings = bool(getattr(target_face, 'assigned_merged_embeddings', {})) and any(getattr(target_face, 'assigned_merged_embeddings', {}))
            # assigned_input_embedding is derived so we check the sources
            if has_input_faces or has_merged_embeddings:
                at_least_one_target_has_input = True
                break

    if not at_least_one_target_has_input:
        reply = QMessageBox.warning(main_window, "Confirm Save", 
                                    "No input faces or merged embedding assigned to ANY target face! No face swaps will happen for this job. Proceed anyway?",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.No:
            return

    dialog = widget_components.SaveJobDialog(main_window)
    if dialog.exec() == QtWidgets.QDialog.Accepted:
        job_name = dialog.job_name
        use_job_name_for_output = dialog.use_job_name_for_output
        # Validate job name for universal filesystem safety (Windows/Linux)
        # Only allow alphanumeric, dash, underscore, and space
        if not job_name:
            QMessageBox.warning(main_window, "Invalid Name", "Job name cannot be empty.")
            return
        if not re.match(r'^[\w\- ]+$', job_name):
            QMessageBox.warning(main_window, "Invalid Name", "Job name contains invalid characters. Only letters, numbers, spaces, dashes, and underscores are allowed.")
            return
        save_job(main_window, job_name, use_job_name_for_output)
        refresh_job_list(main_window)

def load_job_by_name(main_window, job_name: str):
    print(f"[DEBUG] load_job_by_name() called with job_name='{job_name}'")
    if not job_name:
        QMessageBox.warning(main_window, "No Job Name", "No job name provided.")
        return
    print(f"[DEBUG] About to call load_job_workspace for '{job_name}'")
    load_job_workspace(main_window, job_name)
    print(f"[DEBUG] load_job_workspace call returned for '{job_name}'")

def start_recording(main_window):
    print("[DEBUG] MainWindow.start_recording() called.")
    if not main_window.buttonMediaRecord.isChecked():
        print("[DEBUG] buttonMediaRecord is not checked; toggling it now...")
        main_window.buttonMediaRecord.click()
    else:
        print("[DEBUG] Already in recording mode; skipping toggle.")

def connect_job_manager_signals(main_window):
    """Connect Job Manager UI buttons to job actions."""
    main_window.addJobButton.clicked.connect(lambda: prompt_job_name(main_window))
    main_window.deleteJobButton.clicked.connect(lambda: delete_job(main_window))
    if main_window.loadJobButton:
        main_window.loadJobButton.clicked.connect(lambda: load_job(main_window))

def refresh_job_list(main_window):
    """Updates the job queue list with the latest job files."""
    main_window.jobQueueList.clear()
    job_names = list_jobs()
    main_window.jobQueueList.addItems(job_names)
    update_job_manager_buttons(main_window)

def get_selected_job(main_window):
    """Gets the currently selected job from the job list."""
    selected_item = main_window.jobQueueList.currentItem()
    return selected_item.text() if selected_item else None

def get_selected_jobs(main_window):
    """Returns a list of selected job names from the job list widget."""
    selected_items = main_window.jobQueueList.selectedItems()
    return [item.text() for item in selected_items] if selected_items else []

def process_selected_job(main_window: "MainWindow"):
    """Process only the selected jobs in the job list."""
    selected_jobs = get_selected_jobs(main_window)
    if not selected_jobs:
        QtWidgets.QMessageBox.warning(main_window, "No Job Selected", "Please select one or more jobs to process.")
        return
    print(f"[DEBUG] Processing selected jobs: {selected_jobs}")
    main_window.job_processor = JobProcessor(main_window, jobs_to_process=selected_jobs)
    main_window.job_processor.load_job_signal.connect(lambda job_name: load_job_by_name(main_window, job_name))
    main_window.job_processor.start_recording_signal.connect(lambda: start_recording(main_window))
    main_window.job_processor.job_completed_signal.connect(lambda: refresh_job_list(main_window))
    main_window.job_processor.all_jobs_done_signal.connect(
        lambda: QtWidgets.QMessageBox.information(
            main_window, "Job Processing", "Selected jobs completed."
        )
    )
    main_window.job_processor.start()

class JobProcessor(QThread):
    job_completed_signal = Signal(str)
    all_jobs_done_signal = Signal()
    load_job_signal = Signal(str)
    start_recording_signal = Signal()

    def __init__(self, main_window: "MainWindow", jobs_to_process=None):
        super().__init__()
        self.main_window = main_window
        self.jobs_dir = os.path.join(os.getcwd(), "jobs")
        self.completed_dir = os.path.join(self.jobs_dir, "completed")
        if jobs_to_process is not None:
            self.jobs = jobs_to_process
        else:
            self.jobs = list_jobs()
        self.current_job = None

        if not os.path.exists(self.completed_dir):
            os.makedirs(self.completed_dir)

    def run(self):
        print("[DEBUG] Entering JobProcessor.run()...")

        if not self.jobs:
            print("[DEBUG] No jobs to process. Exiting run().")
            return

        for job_name in self.jobs:
            self.current_job = job_name
            print(f"[DEBUG] Beginning processing on job: {job_name}")

            # 1) Load the job (in main UI thread)
            print(f"[DEBUG] Emitting load_job_signal('{job_name}')")
            # Clear the event before loading
            job_loaded_event.clear()
            self.load_job_signal.emit(job_name)

            # 2) Wait for load_job_workspace to finish
            job_loaded_event.wait()
            print("[DEBUG] job_loaded_event received!")
            #self.msleep(5_000)

            # 3) Emit signal to start recording
            print("[DEBUG] Emitting start_recording_signal()")
            self.start_recording_signal.emit()

            # 4) Wait 1 second so the record button has time to toggle
            self.msleep(1_000)

            # 5) Now monitor for recording to finish
            print("[DEBUG] Calling wait_for_recording_to_complete()...")
            self.wait_for_recording_to_complete()

            # Move the job file to 'completed'
            job_path = os.path.join(self.jobs_dir, f"{job_name}.json")
            completed_path = os.path.join(self.completed_dir, f"{job_name}.json")

            if os.path.exists(job_path):
                shutil.move(job_path, completed_path)
                print(f"[DEBUG] Moved job '{job_name}' to completed folder.")
                self.job_completed_signal.emit(job_name)
            else:
                print(f"[ERROR] Job file not found: {job_path}. Skipping move operation.")

        print("[DEBUG] Finished processing all jobs, emitting all_jobs_done_signal()")
        self.all_jobs_done_signal.emit()

    def wait_for_recording_to_complete(self):
        """Waits until video processing has stopped by monitoring the UI state."""
        print(f"[DEBUG] wait_for_recording_to_complete() checking self.main_window.video_processor.recording...")
        if self.main_window.video_processor.recording:
            print("[DEBUG] Recording in progress...")
        while self.main_window.video_processor.recording:
            self.msleep(5000)
        print(f"[DEBUG] Processing stopped for job: {self.current_job}")

def start_processing_all_jobs(main_window: "MainWindow"):
    """Starts processing all jobs in sequence."""
    print("[DEBUG] Entered start_processing_all_jobs...")
    # Create and store new JobProcessor instance
    main_window.job_processor = JobProcessor(main_window)
    
    print("[DEBUG] Connecting signals in start_processing_all_jobs...")
    # Connect the necessary signals
    main_window.job_processor.load_job_signal.connect(lambda job_name: load_job_by_name(main_window, job_name))
    main_window.job_processor.start_recording_signal.connect(lambda: start_recording(main_window))
    main_window.job_processor.job_completed_signal.connect(lambda: refresh_job_list(main_window))
    main_window.job_processor.all_jobs_done_signal.connect(
        lambda: QtWidgets.QMessageBox.information(
            main_window, "Job Processing", "All jobs completed."
        )
    )

    print("[DEBUG] About to start job_processor thread...")
    main_window.job_processor.start()
    print("[DEBUG] Exiting start_processing_all_jobs...")
