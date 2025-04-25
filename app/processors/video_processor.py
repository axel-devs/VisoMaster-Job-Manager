import threading
import queue
from typing import TYPE_CHECKING, Dict, Tuple
import time
import subprocess
from pathlib import Path
import os
import gc
from functools import partial
import tempfile
import shutil
import uuid

import cv2
import numpy
import torch
import pyvirtualcam

from PySide6.QtCore import QObject, QTimer, Signal, Slot, QEventLoop
from PySide6.QtGui import QPixmap
from app.processors.workers.frame_worker import FrameWorker
from app.ui.widgets.actions import graphics_view_actions
from app.ui.widgets.actions import common_actions as common_widget_actions

from app.ui.widgets.actions import video_control_actions
from app.ui.widgets.actions import layout_actions
import app.helpers.miscellaneous as misc_helpers

if TYPE_CHECKING:
    from app.ui.main_ui import MainWindow

class VideoProcessor(QObject):
    frame_processed_signal = Signal(int, QPixmap, numpy.ndarray)
    webcam_frame_processed_signal = Signal(QPixmap, numpy.ndarray)
    single_frame_processed_signal = Signal(int, QPixmap, numpy.ndarray)
    start_segment_timers_signal = Signal(int)
    recording_actually_started = Signal()
    def __init__(self, main_window: 'MainWindow', num_threads=2):
        super().__init__()
        self.main_window = main_window
        self.frame_queue = queue.Queue(maxsize=num_threads)
        self.media_capture: cv2.VideoCapture|None = None
        self.file_type = None
        self.fps = 0
        self.processing = False # General flag if any processing (play or segment) is active
        self.current_frame_number = 0
        self.max_frame_number = 0
        self.media_path = None
        self.num_threads = num_threads
        self.threads: Dict[int, threading.Thread] = {}

        self.current_frame: numpy.ndarray = []
        # self.recording = False # Replaced by is_processing_segments

        self.virtcam: pyvirtualcam.Camera|None = None

        self.recording_sp: subprocess.Popen|None = None
        # self.temp_file = '' # Replaced by segment-specific files

        #Used to calculate the total processing time
        self.start_time = 0.0
        self.end_time = 0.0

        # Used for simple playback, not segment recording
        self.play_start_time = 0.0
        self.play_end_time = 0.0

        # --- Timers ---
        self.frame_read_timer = QTimer()
        # Connect timers later based on mode (play vs record segment)
        self.frame_display_timer = QTimer()

        self.next_frame_to_display = 0
        self.frame_processed_signal.connect(self.store_frame_to_display)
        self.frames_to_display: Dict[int, Tuple[QPixmap, numpy.ndarray]] = {}

        self.webcam_frame_processed_signal.connect(self.store_webcam_frame_to_display)
        self.webcam_frames_to_display = queue.Queue()

        self.gpu_memory_update_timer = QTimer()
        self.gpu_memory_update_timer.timeout.connect(partial(common_widget_actions.update_gpu_memory_progressbar, main_window))

        self.single_frame_processed_signal.connect(self.display_current_frame)

        # --- Multi-Segment Recording State ---
        self.segments_to_process: list[tuple[int, int]] = []
        self.current_segment_index: int = -1
        self.temp_segment_files: list[str] = []
        self.current_segment_end_frame: int | None = None
        self.is_processing_segments: bool = False
        self.segment_temp_dir: str | None = None
        self.triggered_by_job_manager: bool = False # Flag to indicate source of recording trigger
        # --- End Multi-Segment State ---

    Slot(int, QPixmap, numpy.ndarray)
    def store_frame_to_display(self, frame_number, pixmap, frame):
        # print("Called store_frame_to_display()")
        self.frames_to_display[frame_number] = (pixmap, frame)

    # Use a queue to store the webcam frames, since the order of frames is not that important (Unless there are too many threads)
    Slot(QPixmap, numpy.ndarray)
    def store_webcam_frame_to_display(self, pixmap, frame):
        # print("Called store_webcam_frame_to_display()")
        self.webcam_frames_to_display.put((pixmap, frame))

    Slot(int, QPixmap, numpy.ndarray)
    def display_current_frame(self, frame_number, pixmap, frame):
        if self.main_window.loading_new_media:
            graphics_view_actions.update_graphics_view(self.main_window, pixmap, frame_number, reset_fit=True)
            self.main_window.loading_new_media = False

        else:
            graphics_view_actions.update_graphics_view(self.main_window, pixmap, frame_number,)
        self.current_frame = frame
        torch.cuda.empty_cache()
        #Set GPU Memory Progressbar
        common_widget_actions.update_gpu_memory_progressbar(self.main_window)
    def display_next_frame(self):
        should_stop = False
        if not self.processing:
            should_stop = True
        elif self.is_processing_segments:
            if self.current_segment_end_frame is not None and self.next_frame_to_display > self.current_segment_end_frame:
                print(f"Segment {self.current_segment_index + 1} end frame ({self.current_segment_end_frame}) reached.")
                self.stop_current_segment()
                return
        elif self.next_frame_to_display > self.max_frame_number:
            print("End of video reached during playback.")
            should_stop = True

        if should_stop:
            self.stop_processing()
            return

        if self.next_frame_to_display not in self.frames_to_display:
            return
        else:
            pixmap, frame = self.frames_to_display.pop(self.next_frame_to_display)
            self.current_frame = frame

            self.send_frame_to_virtualcam(frame)

            if self.is_processing_segments:
                if self.recording_sp and self.recording_sp.stdin and not self.recording_sp.stdin.closed:
                    try:
                        self.recording_sp.stdin.write(frame.tobytes())
                    except OSError as e:
                        print(f"[WARN] Error writing frame {self.next_frame_to_display} to FFmpeg stdin for segment {self.current_segment_index + 1}: {e}")
                else:
                     print(f"[WARN] FFmpeg stdin not available for segment {self.current_segment_index + 1} when trying to write frame {self.next_frame_to_display}.")

            if not self.is_processing_segments:
                video_control_actions.update_widget_values_from_markers(self.main_window, self.next_frame_to_display)
            graphics_view_actions.update_graphics_view(self.main_window, pixmap, self.next_frame_to_display)
            
            if self.next_frame_to_display in self.threads:
                 self.threads.pop(self.next_frame_to_display)
            self.next_frame_to_display += 1

    def display_next_webcam_frame(self):
        # print("Called display_next_webcam_frame()")
        if not self.processing:
            self.stop_processing()
        if self.webcam_frames_to_display.empty():
            # print("No Webcam frame found to display")
            return
        else:
            pixmap, frame = self.webcam_frames_to_display.get()
            self.current_frame = frame
            self.send_frame_to_virtualcam(frame)
            graphics_view_actions.update_graphics_view(self.main_window, pixmap, 0)

    def send_frame_to_virtualcam(self, frame: numpy.ndarray):
        if self.main_window.control['SendVirtCamFramesEnableToggle'] and self.virtcam:
            # Check if the dimensions of the frame matches that of the Virtcam object
            # If it doesn't match, reinstantiate the Virtcam object with new dimensions
            height, width, _ = frame.shape
            if self.virtcam.height!=height or self.virtcam.width!=width:
                self.enable_virtualcam()
            try:
                self.virtcam.send(frame)
                self.virtcam.sleep_until_next_frame()
            except Exception as e:
                print(f"[WARN] Failed sending frame to virtualcam: {e}")

    def set_number_of_threads(self, value):
        self.stop_processing()
        self.main_window.models_processor.set_number_of_threads(value)
        self.num_threads = value
        self.frame_queue = queue.Queue(maxsize=self.num_threads)
        print(f"Max Threads set as {value} ")

    def process_video(self): # Playback only
        """Start simple video playback processing."""
        if self.processing or self.is_processing_segments:
            print("Processing already in progress (play or segment). Ignoring start request.")
            return

        if self.file_type != 'video':
             print("process_video: Only applicable for video files.")
             return

        if not (self.media_capture and self.media_capture.isOpened()):
            print("Error: Unable to open the video for playback.")
            self.processing = False
            video_control_actions.reset_media_buttons(self.main_window)
            return

        print("Starting video playback processing setup...") # Commented out
        self.processing = True # General processing flag
        self.start_time = time.perf_counter()
        self.frames_to_display.clear()
        self.threads.clear()

        # Calculate start time based on current slider position
        self.play_start_time = float(self.media_capture.get(cv2.CAP_PROP_POS_FRAMES) / float(self.fps)) if self.fps > 0 else 0.0
        self.current_frame_number = self.main_window.videoSeekSlider.value()
        self.next_frame_to_display = self.current_frame_number

        # --- Start Timers ---
        # Signals are connected here. Disconnection happens in stop_processing.
        self.frame_display_timer.timeout.connect(self.display_next_frame)
        self.frame_read_timer.timeout.connect(self.process_next_frame)

        if self.main_window.control['VideoPlaybackCustomFpsToggle']:
            fps = self.main_window.control['VideoPlaybackCustomFpsSlider']
        else:
            fps = self.media_capture.get(cv2.CAP_PROP_FPS)

        interval = 1000 / fps if fps > 0 else 30
        interval = int(interval * 0.8)
        print(f"Starting playback timers with interval {interval} ms.")
        self.frame_read_timer.start(interval)
        self.frame_display_timer.start()
        self.gpu_memory_update_timer.start(5000)

    def process_next_frame(self):
        """Read the next frame and enqueue for processing."""
        if not self.processing:
            self.frame_read_timer.stop()
            return
        if self.is_processing_segments:
             if self.current_segment_end_frame is not None and self.current_frame_number > self.current_segment_end_frame:
                  self.frame_read_timer.stop() # Stop reading for this segment
                  # Display logic will handle calling stop_current_segment when the display catches up
                  return
        elif self.current_frame_number > self.max_frame_number:
             # End of video reached during normal playback
             self.frame_read_timer.stop()
             # Display logic will call stop_processing
             return

        if self.frame_queue.qsize() >= self.num_threads:
            return

        if self.file_type == 'video' and self.media_capture:
            ret, frame = misc_helpers.read_frame(self.media_capture, preview_mode = not self.is_processing_segments)
            if ret:
                frame = frame[..., ::-1]  # Convert BGR to RGB
                self.frame_queue.put(self.current_frame_number)
                self.start_frame_worker(self.current_frame_number, frame)
                self.current_frame_number += 1
            else:
                # Frame read failed!
                failed_frame_num = self.current_frame_number
                print(f"[ERROR] Cannot read frame {failed_frame_num}! Video source may be corrupted.")
                self.frame_read_timer.stop() # Stop trying to read more frames immediately
                
                if self.is_processing_segments:
                    print(f"Attempting to finalize recording segments due to read error at frame {failed_frame_num}.")
                    # Stop the current segment gracefully, which will then trigger concatenation
                    self.stop_current_segment()
                else:
                    # For normal playback, just stop everything
                    self.stop_processing()

    def start_frame_worker(self, frame_number, frame, is_single_frame=False):
        """Start a FrameWorker to process the given frame."""
        worker = FrameWorker(frame, self.main_window, frame_number, self.frame_queue, is_single_frame)
        self.threads[frame_number] = worker
        if is_single_frame:
            worker.run()
        else:
            worker.start()

    def process_current_frame(self):

        self.next_frame_to_display = self.current_frame_number
        if self.file_type == 'video' and self.media_capture:
            ret, frame = misc_helpers.read_frame(self.media_capture, preview_mode=False)
            if ret:
                frame = frame[..., ::-1]  # Convert BGR to RGB
                self.frame_queue.put(self.current_frame_number)
                self.start_frame_worker(self.current_frame_number, frame, is_single_frame=True)
                
                self.media_capture.set(cv2.CAP_PROP_POS_FRAMES, self.current_frame_number)
            else:
                # Frame read failed during single frame processing (e.g., after seek)
                self.main_window.last_seek_read_failed = True 

        elif self.file_type == 'image':
            frame = misc_helpers.read_image_file(self.media_path)
            if frame is not None:
                frame = frame[..., ::-1]  # Convert BGR to RGB
                self.frame_queue.put(self.current_frame_number)
                self.start_frame_worker(self.current_frame_number, frame, is_single_frame=True)
            else:
                print("Error: Unable to read image file.")

        elif self.file_type == 'webcam':
            ret, frame = misc_helpers.read_frame(self.media_capture, preview_mode = False)
            if ret:
                frame = frame[..., ::-1]  # Convert BGR to RGB
                self.frame_queue.put(self.current_frame_number)
                self.start_frame_worker(self.current_frame_number, frame, is_single_frame=True)
            else:
                print("Unable to read Webcam frame!")
        
        # Wait for the single frame worker to complete
        self.join_and_clear_threads()

    def process_next_webcam_frame(self):
        # print("Called process_next_webcam_frame()")

        if self.frame_queue.qsize() >= self.num_threads:
            # print(f"Queue is full ({self.frame_queue.qsize()} frames). Throttling frame reading.")
            return
        if self.file_type == 'webcam' and self.media_capture:
            ret, frame = misc_helpers.read_frame(self.media_capture, preview_mode = False)
            if ret:
                frame = frame[..., ::-1]  # Convert BGR to RGB
                # print(f"Enqueuing frame {self.current_frame_number}")
                self.frame_queue.put(self.current_frame_number)
                self.start_frame_worker(self.current_frame_number, frame)

    # @misc_helpers.benchmark
    def stop_processing(self, stopped_by_end_frame=False): # General stop/abort
        """Stop any active processing (playback or segment recording). Handles cleanup."""
        if not self.processing and not self.is_processing_segments:
            # print("No processing active to stop.")
            video_control_actions.reset_media_buttons(self.main_window)
            return False # Nothing was stopped
        
        print("stop_processing called: Aborting active processing...")
        
        was_processing_segments = self.is_processing_segments
        self.processing = False
        self.is_processing_segments = False
        self.triggered_by_job_manager = False # Reset flag on abort
        
        # Stop timers immediately
        self.frame_read_timer.stop()
        self.frame_display_timer.stop()
        self.gpu_memory_update_timer.stop()

        # Wait for worker threads to finish (important!)
        self.join_and_clear_threads()
        
        # Clear display queues
        self.frames_to_display.clear()
        self.webcam_frames_to_display.queue.clear()
        with self.frame_queue.mutex:
            self.frame_queue.queue.clear()

        # Stop and cleanup any active ffmpeg subprocess
        if self.recording_sp:
            print("Closing and waiting for active FFmpeg subprocess...")
            if self.recording_sp.stdin and not self.recording_sp.stdin.closed:
                 try: self.recording_sp.stdin.close()
                 except OSError: pass
            self.recording_sp.wait()
            self.recording_sp = None
        
        # Cleanup temporary segment directory if it exists
        self._cleanup_temp_dir()

        # Reset state variables
        self.segments_to_process = []
        self.current_segment_index = -1
        self.temp_segment_files = []
        self.current_segment_end_frame = None

        # Reset capture position to slider
        if self.media_capture:
             current_slider_pos = self.main_window.videoSeekSlider.value()
             self.current_frame_number = current_slider_pos
             self.next_frame_to_display = current_slider_pos
             self.media_capture.set(cv2.CAP_PROP_POS_FRAMES, current_slider_pos)

        # Re-enable UI elements if we were processing segments
        if was_processing_segments:
            layout_actions.enable_all_parameters_and_control_widget(self.main_window)
        
        # print("Clearing Cache") # Commented out
        try:
            import torch
            if torch.cuda.is_available(): torch.cuda.empty_cache()
        except ImportError: pass
        gc.collect()
        
        video_control_actions.reset_media_buttons(self.main_window)
        print("Processing finished and cleaned up.")
        return True # Processing was stopped

    def join_and_clear_threads(self):
        # print("Joining Threads")
        for _, thread in self.threads.items():
            if thread.is_alive():
                thread.join()
        # print('Clearing Threads')
        self.threads.clear()
    
    def create_ffmpeg_subprocess(self, output_filename: str):
        if not isinstance(self.current_frame, numpy.ndarray) or self.current_frame.size == 0:
             print("[ERROR] create_ffmpeg_subprocess: Invalid or empty self.current_frame. Cannot determine dimensions.")
             return False
        if not self.media_path or not Path(self.media_path).is_file():
             print("[ERROR] create_ffmpeg_subprocess: Original media path is invalid or not set.")
             return False
        if self.fps <= 0:
             print("[ERROR] create_ffmpeg_subprocess: Invalid FPS, cannot calculate segment times.")
             return False

        # Get current segment details
        if self.current_segment_index < 0 or self.current_segment_index >= len(self.segments_to_process):
            print(f"[ERROR] create_ffmpeg_subprocess: Invalid segment index {self.current_segment_index}.")
            return False
        start_frame, end_frame = self.segments_to_process[self.current_segment_index]
        
        # Calculate timecodes for the audio segment
        start_time_sec = start_frame / self.fps
        end_time_sec = end_frame / self.fps # Use end time for -to
        # Alternatively, calculate duration: duration_sec = (end_frame - start_frame + 1) / self.fps -> use with -t

        frame_height, frame_width, _ = self.current_frame.shape
        print(f"Creating FFmpeg subprocess: Video Dim={frame_width}x{frame_height}, FPS={self.fps}, Output='{output_filename}'")
        print(f"                            Audio Segment: Start={start_time_sec:.3f}s, End={end_time_sec:.3f}s")

        if Path(output_filename).is_file():
            try:
                os.remove(output_filename)
            except OSError as e:
                 print(f"[WARN] Could not remove existing file {output_filename}: {e}")

        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            
            # Input 0: Processed Video from Pipe
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{frame_width}x{frame_height}",
            "-r", str(self.fps),
            "-i", "pipe:0", # Explicitly label pipe as input 0

            # Input 1: Original Audio from File (Segmented)
            "-ss", str(start_time_sec), # Seek before decoding input
            "-to", str(end_time_sec),   # Specify end time
            "-i", self.media_path,     # Original media file as input 1

            # Mapping: Take video from input 0, audio from input 1
            "-map", "0:v:0",           # Map video from pipe
            "-map", "1:a:0?",          # Map audio from file (optional)

            # Video Codec & Options (as before, potentially adjust for padding/format if needed)
             "-vf", f"pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuvj420p", # Use original filter
            "-c:v", "libx264",
            "-crf", "18",

            # Audio Codec & Options
            "-c:a", "copy",            # Copy original audio stream (lossless)
            
            # Ensure output stops when the shortest input ends (the video pipe)
            "-shortest", 

            # Output File
            output_filename
        ]

        try:
            # Use stdin=subprocess.PIPE for input 0
            self.recording_sp = subprocess.Popen(args, stdin=subprocess.PIPE, bufsize=-1) 
            return True
        except FileNotFoundError:
            print("[ERROR] FFmpeg command not found. Ensure FFmpeg is installed and in system PATH.")
            self.main_window.display_messagebox_signal.emit('FFmpeg Error', 'FFmpeg command not found.', self.main_window)
            return False
        except Exception as e:
             print(f"[ERROR] Failed to start FFmpeg subprocess: {e}")
             self.main_window.display_messagebox_signal.emit('FFmpeg Error', f'Failed to start FFmpeg:\\n{e}', self.main_window)
             return False

    def enable_virtualcam(self, backend=False):
        #Check if capture contains any cv2 stream or is it an empty list
        if self.media_capture:
            if isinstance(self.current_frame, numpy.ndarray):
                frame_height, frame_width, _ = self.current_frame.shape
            else:
                frame_height = int(self.media_capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
                frame_width = int(self.media_capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.disable_virtualcam()
            try:
                backend = backend or self.main_window.control['VirtCamBackendSelection']
                # self.virtcam = pyvirtualcam.Camera(width=vid_width, height=vid_height, fps=int(self.fps), backend='unitycapture', device='Unity Video Capture')
                self.virtcam = pyvirtualcam.Camera(width=frame_width, height=frame_height, fps=int(self.fps), backend=backend, fmt=pyvirtualcam.PixelFormat.BGR)

            except Exception as e:
                print(f"[ERROR] Failed to enable virtual camera: {e}")

    def disable_virtualcam(self):
        if self.virtcam:
            self.virtcam.close()
        self.virtcam = None

    # --- Multi-Segment Recording Methods ---
    def start_multi_segment_recording(self, segments: list[tuple[int, int]], triggered_by_job_manager: bool = False):
        if self.processing or self.is_processing_segments:
            print("[WARN] Attempted to start segment recording while already processing.")
            return

        print("--- Initializing multi-segment recording... ---")
        self.is_processing_segments = True
        self.triggered_by_job_manager = triggered_by_job_manager
        self.segments_to_process = segments
        self.current_segment_index = -1
        self.temp_segment_files = []
        self.segment_temp_dir = None

        try:
            app_root = os.getcwd()
            temp_dir_name = f"visomaster_segments_{uuid.uuid4()}"
            self.segment_temp_dir = os.path.join(app_root, temp_dir_name)
            os.makedirs(self.segment_temp_dir, exist_ok=True)
            # print(f"Created temporary directory for segments: {self.segment_temp_dir}")
        except Exception as e:
            print(f"[ERROR] Failed to create temporary directory: {e}")
            self.main_window.display_messagebox_signal.emit('File System Error', f'Failed to create temporary directory:\n{e}', self.main_window)
            self.stop_processing()
            return
        
        self.start_time = time.perf_counter()
        self.process_next_segment()

    @Slot(int)
    def _start_timers_from_signal(self, interval: int):
        """Slot to start frame read and display timers from the main thread."""
        print(f"[DEBUG] Starting segment processing timers (from signal) with interval {interval} ms.")
        # Ensure the display timer signal is connected before starting
        self.frame_display_timer.timeout.connect(self.display_next_frame)
        self.frame_read_timer.start(interval)
        self.frame_display_timer.start(interval)
        self.processing = True # Set processing flag when timers actually start
        self.recording_actually_started.emit()

    def process_next_segment(self):
        self.current_segment_index += 1
        
        if self.current_segment_index >= len(self.segments_to_process):
            print("All segments processed.")
            self.finalize_segment_concatenation()
            return

        start_frame, end_frame = self.segments_to_process[self.current_segment_index]
        segment_num = self.current_segment_index + 1
        print(f"--- Starting Segment {segment_num}/{len(self.segments_to_process)} (Frames: {start_frame} - {end_frame}) ---")
        
        self.current_segment_end_frame = end_frame

        if not self.media_capture or not self.media_capture.isOpened():
             print(f"[ERROR] Media capture not available for seeking to segment {segment_num}.")
             self.stop_processing()
             return
        print(f"Seeking to start frame {start_frame}...")
        self.current_frame_number = start_frame
        self.next_frame_to_display = start_frame
        self.media_capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        self.main_window.videoSeekSlider.blockSignals(True)
        self.main_window.videoSeekSlider.setValue(start_frame)
        self.main_window.videoSeekSlider.blockSignals(False)
        
        ret, frame_bgr = misc_helpers.read_frame(self.media_capture, preview_mode=False)
        if ret:
            self.current_frame = numpy.ascontiguousarray(frame_bgr[..., ::-1])
        else:
            print(f"[ERROR] Could not read frame {start_frame} at start of segment {segment_num}.")
            self.stop_processing()
            return
        self.media_capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

        temp_segment_filename = f"segment_{self.current_segment_index}.mp4"
        temp_segment_path = os.path.join(self.segment_temp_dir, temp_segment_filename)
        self.temp_segment_files.append(temp_segment_path)
        if not self.create_ffmpeg_subprocess(output_filename=temp_segment_path):
            print(f"[ERROR] Failed to create ffmpeg subprocess for segment {segment_num}.")
            self.stop_processing()
            return

        self.frame_read_timer.timeout.connect(self.process_next_frame)

        fps = self.media_capture.get(cv2.CAP_PROP_FPS)
        interval = 1000 / fps if fps > 0 else 30
        self.start_segment_timers_signal.emit(int(interval))

    def stop_current_segment(self):
        segment_num = self.current_segment_index + 1
        print(f"--- Stopping Segment {segment_num} --- ")
        self.processing = False
        self.frame_read_timer.stop()
        self.frame_display_timer.stop()

        if self.recording_sp and self.recording_sp.stdin:
            if not self.recording_sp.stdin.closed:
                try:
                    print(f"Closing FFmpeg stdin for segment {segment_num}...")
                    self.recording_sp.stdin.close()
                except OSError as e:
                    print(f"[WARN] Error closing FFmpeg stdin for segment {segment_num}: {e}")
            print(f"Waiting for FFmpeg subprocess (segment {segment_num}) to finish...")
            self.recording_sp.wait()
            print(f"FFmpeg subprocess (segment {segment_num}) finished.")
            self.recording_sp = None
        else:
            print(f"[WARN] No active FFmpeg subprocess found when stopping segment {segment_num}.")
        
        if self.temp_segment_files and not os.path.exists(self.temp_segment_files[-1]):
            print(f"[ERROR] Segment file {self.temp_segment_files[-1]} not found after processing segment {segment_num}.")

        self.process_next_segment()

    def finalize_segment_concatenation(self):
        print("--- Finalizing concatenation of segments... ---")
        was_triggered_by_job = self.triggered_by_job_manager
        # Ensure processing flags are false *before* cleanup
        self.is_processing_segments = False 
        self.processing = False

        valid_segments_exist = any(os.path.exists(f) for f in self.temp_segment_files if f)

        if not self.temp_segment_files or not valid_segments_exist:
            print("[WARN] No valid temporary segment files found to concatenate.")
            self._cleanup_temp_dir()
            video_control_actions.reset_media_buttons(self.main_window)
            layout_actions.enable_all_parameters_and_control_widget(self.main_window)
            return

        job_name = getattr(self.main_window, 'current_job_name', None)
        use_job_name_for_output = getattr(self.main_window, 'use_job_name_for_output', False)
        output_file_name = getattr(self.main_window, 'output_file_name', None)
        
        final_file_path = misc_helpers.get_output_file_path(
            self.media_path,
            self.main_window.control['OutputMediaFolder'],
            job_name=job_name if was_triggered_by_job else None,
            use_job_name_for_output=use_job_name_for_output if was_triggered_by_job else False,
            output_file_name=output_file_name if was_triggered_by_job else None
        )
        
        # Ensure output directory exists
        output_dir = os.path.dirname(final_file_path)
        if not os.path.exists(output_dir):
            try:
                os.makedirs(output_dir)
                print(f"Created output directory: {output_dir}")
            except OSError as e:
                print(f"[ERROR] Failed to create output directory {output_dir}: {e}")
                self.main_window.display_messagebox_signal.emit('File Error', f'Could not create output directory:\n{output_dir}\n\n{e}', self.main_window)
                self._cleanup_temp_dir()
                video_control_actions.reset_media_buttons(self.main_window)
                layout_actions.enable_all_parameters_and_control_widget(self.main_window)
                return

        if Path(final_file_path).is_file():
            print(f"Removing existing final file: {final_file_path}")
            try:
                os.remove(final_file_path)
            except OSError as e:
                print(f"[ERROR] Failed to remove existing file {final_file_path}: {e}")
                self.main_window.display_messagebox_signal.emit('File Error', f'Could not delete existing file:\n{final_file_path}\n\n{e}', self.main_window)
                self._cleanup_temp_dir()
                video_control_actions.reset_media_buttons(self.main_window)
                layout_actions.enable_all_parameters_and_control_widget(self.main_window)
                return

        list_file_path = os.path.join(self.segment_temp_dir, "mylist.txt")
        concatenation_successful = False
        try:
            print(f"Creating ffmpeg list file: {list_file_path}")
            segment_count = 0
            with open(list_file_path, 'w') as f_list:
                for segment_path in self.temp_segment_files:
                     if segment_path and os.path.exists(segment_path) and os.path.getsize(segment_path) > 0:
                          abs_path = os.path.abspath(segment_path)
                          formatted_path = abs_path.replace('\\', '/')
                          f_list.write(f"file '{formatted_path}'\n")
                          segment_count += 1
                     else:
                          print(f"[WARN] Segment file missing, empty, or invalid, skipping: {segment_path}")
            
            if segment_count == 0:
                 print("[ERROR] Concatenation list file is empty or contains no valid segments.")
                 raise ValueError("No valid segments to concatenate.")

            print(f"Concatenating {segment_count} valid segments into {final_file_path}...")
            concat_args = [
                "ffmpeg", '-hide_banner', '-loglevel', 'error',
                "-f", "concat",
                "-safe", "0",
                "-i", list_file_path,
                "-c", "copy",
                final_file_path
            ]
            subprocess.run(concat_args, check=True)
            concatenation_successful = True

            if not was_triggered_by_job:
                print(f"--- Successfully created final video: {final_file_path} ---")
            else:
                 print(f"--- Job Manager: Successfully created final video: {final_file_path} ---")

        except subprocess.CalledProcessError as e:
            print(f"[ERROR] FFmpeg command failed during final concatenation: {e}")
            self.main_window.display_messagebox_signal.emit('Recording Error', f'FFmpeg command failed during concatenation:\n{e}\nCould not create final video.', self.main_window)
        except FileNotFoundError:
            print(f"[ERROR] FFmpeg not found. Ensure it's in your system PATH.")
            self.main_window.display_messagebox_signal.emit('Recording Error', f'FFmpeg not found.', self.main_window)
        except Exception as e:
            print(f"[ERROR] An unexpected error occurred during finalization: {e}")
            self.main_window.display_messagebox_signal.emit('Recording Error', f'An unexpected error occurred:\n{e}', self.main_window)
        finally:
            self._cleanup_temp_dir()
            # Reset segment state regardless of success/failure
            self.segments_to_process = []
            self.current_segment_index = -1
            self.temp_segment_files = []
            self.current_segment_end_frame = None
            self.triggered_by_job_manager = False

            self.end_time = time.perf_counter()
            processing_time = self.end_time - self.start_time
            if concatenation_successful:
                print(f"Total segment processing and concatenation finished in {processing_time:.2f} seconds")
            else:
                print(f"Segment processing aborted after {processing_time:.2f} seconds due to error.")
            
            # print("Clearing Cache") # Commented out
            try:
                import torch
                if torch.cuda.is_available(): torch.cuda.empty_cache()
            except ImportError: pass
            gc.collect()
            
            # Always re-enable UI and reset buttons
            layout_actions.enable_all_parameters_and_control_widget(self.main_window)
            video_control_actions.reset_media_buttons(self.main_window)
            # print("Multi-segment processing flow finished.")

    def _cleanup_temp_dir(self):
        if self.segment_temp_dir and os.path.exists(self.segment_temp_dir):
            try:
                # print(f"Cleaning up temporary segment directory: {self.segment_temp_dir}")
                shutil.rmtree(self.segment_temp_dir)
            except OSError as e:
                print(f"[WARN] Failed to delete temporary directory {self.segment_temp_dir}: {e}")
        self.segment_temp_dir = None
    # --- End Multi-Segment Methods ---