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

def save_job(main_window, job_name: str):
    """Saves the current workspace as a job in the 'jobs' directory."""
    data_filename = os.path.join(jobs_dir, f"{job_name}")
    save_job_workspace(main_window, data_filename)
    print(f"[DEBUG] Job saved: {data_filename}")

def list_jobs():
    """Lists all saved jobs from the 'jobs' directory."""
    if not os.path.exists(jobs_dir):
        return []
    return [f.replace('.json', '') for f in os.listdir(jobs_dir) if f.endswith('.json')]

def delete_job(main_window: "MainWindow"):
    """Deletes the selected job from the 'jobs' directory."""  
    selected_item = main_window.jobQueueList.currentItem()
    if not selected_item:
        QtWidgets.QMessageBox.warning(main_window, "No Job Selected", "Please select a job to delete.")
        return False

    job_name = selected_item.text()
    job_file = os.path.join(jobs_dir, f"{job_name}.json")

    if os.path.exists(job_file):
        os.remove(job_file)
        print(f"[DEBUG] Job deleted: {job_file}")
        main_window.refresh_job_list()
        return True

    QtWidgets.QMessageBox.warning(main_window, "Job Not Found", f"The job '{job_name}' does not exist.")
    return False

def load_job(main_window, job_name: str):
    """Loads a saved job into VisoMaster."""
    job_file = os.path.join(jobs_dir, f"{job_name}.json")
    if not Path(job_file).is_file():
        print(f"[ERROR] Job file not found: {job_file}")
        return
    load_job_workspace(main_window, job_file)
    print(f"[DEBUG] Loaded job: {job_file}")

def load_job_workspace(main_window: "MainWindow", job_name: str):
    print("[DEBUG] Loading job workspace...")
    jobs_dir = os.path.join(os.getcwd(), "jobs"); os.makedirs(jobs_dir, exist_ok=True)
    data_filename = os.path.join(jobs_dir, f"{job_name}.json")
    if not Path(data_filename).is_file():
        print(f"[DEBUG] No valid file found for job: {job_name}."); return
    with open(data_filename, 'r') as data_file: data = json.load(data_file)
    list_view_actions.clear_stop_loading_input_media(main_window)
    list_view_actions.clear_stop_loading_target_media(main_window)
    main_window.target_videos = {}
    card_actions.clear_input_faces(main_window)
    card_actions.clear_target_faces(main_window)
    card_actions.clear_merged_embeddings(main_window)
    target_medias_data = data.get('target_medias_data', [])
    target_medias_files_list, target_media_ids = zip(*[(m['media_path'], m['media_id']) for m in target_medias_data]) if target_medias_data else ([], [])
    main_window.video_loader_worker = ui_workers.TargetMediaLoaderWorker(main_window=main_window, folder_name=False, files_list=target_medias_files_list, media_ids=target_media_ids)
    main_window.video_loader_worker.thumbnail_ready.connect(partial(list_view_actions.add_media_thumbnail_to_target_videos_list, main_window))
    main_window.video_loader_worker.run()
    selected_media_id = data.get('selected_media_id', False)
    if selected_media_id and main_window.target_videos.get(selected_media_id): main_window.target_videos[selected_media_id].click()
    input_faces_data = data.get('input_faces_data', {})
    input_media_paths, input_face_ids = zip(*[(f['media_path'], face_id) for face_id, f in input_faces_data.items()]) if input_faces_data else ([], [])
    main_window.input_faces_loader_worker = ui_workers.InputFacesLoaderWorker(main_window=main_window, folder_name=False, files_list=input_media_paths, face_ids=input_face_ids)
    main_window.input_faces_loader_worker.thumbnail_ready.connect(partial(list_view_actions.add_media_thumbnail_to_source_faces_list, main_window))
    main_window.input_faces_loader_worker.finished.connect(partial(common_widget_actions.refresh_frame, main_window))
    main_window.input_faces_loader_worker.files_list = list(main_window.input_faces_loader_worker.files_list)
    main_window.input_faces_loader_worker.run()
    for embedding_id, embedding_data in data.get('embeddings_data', {}).items():
        embedding_store = {embed_model: np.array(embed) for embed_model, embed in embedding_data['embedding_store'].items()}
        list_view_actions.create_and_add_embed_button_to_list(main_window, embedding_data['embedding_name'], embedding_store, embedding_id=embedding_id)
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
    for control_name, control_value in data.get('control', {}).items():
        main_window.control[control_name] = control_value
    swap_faces_state = data.get('swap_faces_enabled', True)
    main_window.swapfacesButton.setChecked(swap_faces_state)
    if swap_faces_state:
        video_control_actions.process_swap_faces(main_window)
    print(f"[DEBUG] Swap Faces button state restored: {swap_faces_state}")
    video_control_actions.remove_all_markers(main_window)
    data['markers'] = convert_markers_to_job_type(main_window, data.get('markers', {}), misc_helpers.ParametersDict)
    for marker_position, marker_data in data['markers'].items():
        video_control_actions.add_marker(main_window, marker_data['parameters'], marker_data['control'], int(marker_position))
    main_window.last_target_media_folder_path = data.get('last_target_media_folder_path', '')
    main_window.last_input_media_folder_path = data.get('last_input_media_folder_path', '')
    main_window.loaded_embedding_filename = data.get('loaded_embedding_filename', '')
    common_widget_actions.set_control_widgets_values(main_window)
    output_folder = data.get('control', {}).get('OutputMediaFolder', '')
    common_widget_actions.create_control(main_window, 'OutputMediaFolder', output_folder)
    main_window.outputFolderLineEdit.setText(output_folder)
    layout_actions.fit_image_to_view_onchange(main_window)
    if main_window.target_faces: list(main_window.target_faces.values())[0].click()
    else:
        main_window.current_widget_parameters = data.get('current_widget_parameters', main_window.default_parameters.copy())
        main_window.current_widget_parameters = misc_helpers.ParametersDict(main_window.current_widget_parameters, main_window.default_parameters)
        common_widget_actions.set_widgets_values_using_face_id_parameters(main_window, face_id=False)
    print(f"[DEBUG] Loaded workspace from: {data_filename}")

def save_job_workspace(main_window: "MainWindow", job_name: str):
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
    swap_faces_state = main_window.swapfacesButton.isChecked()
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
        'swap_faces_enabled': swap_faces_state
    }
    with open(data_filename, 'w') as data_file:
        json.dump(save_data, data_file, indent=4)
    print(f"[DEBUG] Job successfully saved to: {data_filename}")

def process_selected_job(main_window: "MainWindow"):
    """Placeholder function for processing a selected job."""
    print("[DEBUG] Selected job processing not implemented yet.")
    QtWidgets.QMessageBox.information(main_window, "Work in Progress", "Processing a selected job is not implemented yet.")

def start_processing_all_jobs(main_window: "MainWindow"):
    """Starts processing all jobs in sequence."""
    print("[DEBUG] Entered start_processing_all_jobs...")
    # Create and store new JobProcessor instance
    main_window.job_processor = JobProcessor(main_window)
    
    print("[DEBUG] Connecting signals in start_processing_all_jobs...")
    # Connect the necessary signals
    main_window.job_processor.load_job_signal.connect(main_window.load_job_by_name)
    main_window.job_processor.start_recording_signal.connect(main_window.start_recording)
    main_window.job_processor.job_completed_signal.connect(main_window.refresh_job_list)
    main_window.job_processor.all_jobs_done_signal.connect(
        lambda: QtWidgets.QMessageBox.information(
            main_window, "Job Processing", "All jobs completed."
        )
    )

    print("[DEBUG] About to start job_processor thread...")
    main_window.job_processor.start()
    print("[DEBUG] Exiting start_processing_all_jobs...")

class JobProcessor(QThread):
    job_completed_signal = Signal(str)
    all_jobs_done_signal = Signal()
    load_job_signal = Signal(str)
    start_recording_signal = Signal()

    def __init__(self, main_window: "MainWindow"):
        super().__init__()
        self.main_window = main_window
        self.jobs_dir = os.path.join(os.getcwd(), "jobs")
        self.completed_dir = os.path.join(self.jobs_dir, "completed")
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
            self.load_job_signal.emit(job_name)

            # 2) Wait 10 seconds to ensure the job workspace has time to load
            print("[DEBUG] Sleeping 10 seconds to let workspace load...")
            self.msleep(10_000)

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
        while self.main_window.video_processor.recording:
            print("[DEBUG] Recording in progress... for sleeping 5s.")
            self.msleep(5000)
        print(f"[DEBUG] Processing stopped for job: {self.current_job}")
