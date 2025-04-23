import threading
import queue
from typing import TYPE_CHECKING, Dict, Tuple
import time
import subprocess
from pathlib import Path
import os
import gc
from functools import partial

import cv2
import numpy
import torch
import pyvirtualcam

from PySide6.QtCore import QObject, QTimer, Signal, Slot
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
    def __init__(self, main_window: 'MainWindow', num_threads=2):
        super().__init__()
        self.main_window = main_window
        self.frame_queue = queue.Queue(maxsize=num_threads)
        self.media_capture: cv2.VideoCapture|None = None
        self.file_type = None
        self.fps = 0
        self.processing = False
        self.current_frame_number = 0
        self.max_frame_number = 0
        self.media_path = None
        self.num_threads = num_threads
        self.threads: Dict[int, threading.Thread] = {}

        self.current_frame: numpy.ndarray = []
        self.recording = False

        self.virtcam: pyvirtualcam.Camera|None = None

        self.recording_sp: subprocess.Popen|None = None 
        self.temp_file = '' 
        #Used to calculate the total processing time
        self.start_time = 0.0
        self.end_time = 0.0

        #Used to store the video start and enc seek time
        self.play_start_time = 0.0
        self.play_end_time = 0.0

        # Timer to manage frame reading intervals
        self.frame_read_timer = QTimer()
        self.frame_read_timer.timeout.connect(self.process_next_frame)

        self.next_frame_to_display = 0
        self.frame_processed_signal.connect(self.store_frame_to_display)
        self.frame_display_timer = QTimer()
        self.frame_display_timer.timeout.connect(self.display_next_frame)
        self.frames_to_display: Dict[int, Tuple[QPixmap, numpy.ndarray]] = {}


        self.webcam_frame_processed_signal.connect(self.store_webcam_frame_to_display)
        self.webcam_frames_to_display = queue.Queue()

        # Timer to update the gpu memory usage progressbar 
        self.gpu_memory_update_timer = QTimer()
        self.gpu_memory_update_timer.timeout.connect(partial(common_widget_actions.update_gpu_memory_progressbar, main_window))

        self.single_frame_processed_signal.connect(self.display_current_frame)

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
        if not self.processing or (self.next_frame_to_display > self.max_frame_number):
            self.stop_processing()
        if self.next_frame_to_display not in self.frames_to_display:
            return
        else:
            pixmap, frame = self.frames_to_display.pop(self.next_frame_to_display)
            self.current_frame = frame

            # Check and send the frame to virtualcam, if the option is selected
            self.send_frame_to_virtualcam(frame)

            # --- Job End Frame Check (Moved Here) ---
            if self.recording and self.main_window.job_end_frame is not None:
                # Stop *before* writing frame > end_frame
                if self.next_frame_to_display > self.main_window.job_end_frame:
                    print(f"Job End Frame ({self.main_window.job_end_frame}) reached. Stopping recording.")
                    self.stop_processing(stopped_by_end_frame=True)
                    # We might have popped the frame already, but we prevent writing it and stop.
                    return 
            # --- End Job End Frame Check ---

            if self.recording:
                # Check if stdin is still available before writing
                if self.recording_sp and self.recording_sp.stdin and not self.recording_sp.stdin.closed:
                    try:
                        self.recording_sp.stdin.write(frame.tobytes())
                    except OSError as e:
                        print(f"[WARN] Error writing frame {self.next_frame_to_display} to FFmpeg stdin: {e}")
                        # Optionally stop processing here if writing fails consistently
                        # self.stop_processing()
                        # return
                else:
                     print(f"[WARN] FFmpeg stdin not available when trying to write frame {self.next_frame_to_display}. Stop might be in progress.")

            # Update the widget values using parameters if it is not recording (The updation of actual parameters is already done inside the FrameWorker, this step is to make the changes appear in the widgets)
            if not self.recording:
                video_control_actions.update_widget_values_from_markers(self.main_window, self.next_frame_to_display)
            graphics_view_actions.update_graphics_view(self.main_window, pixmap, self.next_frame_to_display)
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
                print(e)

    def set_number_of_threads(self, value):
        self.stop_processing()
        self.main_window.models_processor.set_number_of_threads(value)
        self.num_threads = value
        self.frame_queue = queue.Queue(maxsize=self.num_threads)
        print(f"Max Threads set as {value} ")

    def process_video(self):
        """Start video processing by reading frames and enqueueing them."""
        if self.processing:
            print("Processing already in progress. Ignoring start request.")
            return
            
        # Re-initialize the timers
        self.frame_display_timer = QTimer()
        self.frame_read_timer = QTimer()

        if self.file_type == 'video':
            if not (self.media_capture and self.media_capture.isOpened()):
                print("Error: Unable to open the video.")
                self.processing = False
                video_control_actions.reset_media_buttons(self.main_window)
                return

            print("Starting video processing setup...")
            self.processing = True # Set processing flag early
            self.start_time = time.perf_counter()
            self.frames_to_display.clear()
            self.threads.clear()

            # --- Recording Setup (Seek, Start Time, FFmpeg) ---
            if self.recording:
                layout_actions.disable_all_parameters_and_control_widget(self.main_window)
                
                # 1. Handle potential seek to start frame
                start_frame = self.main_window.job_start_frame
                if start_frame is not None and start_frame >= 0:
                    print(f"process_video: Recording mode - Seeking to Job Start Frame {start_frame}")
                    self.current_frame_number = start_frame
                    self.next_frame_to_display = start_frame
                    self.media_capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
                    self.main_window.videoSeekSlider.blockSignals(True) # Avoid triggering on_change
                    self.main_window.videoSeekSlider.setValue(start_frame)
                    self.main_window.videoSeekSlider.blockSignals(False)
                    # Read the frame at the new position to update view and self.current_frame
                    ret, frame_bgr = misc_helpers.read_frame(self.media_capture, preview_mode=False)
                    if ret:
                        # Ensure BGR frame is contiguous for QPixmap conversion
                        frame_bgr_cont = numpy.ascontiguousarray(frame_bgr) 
                        # Convert to RGB and make contiguous for self.current_frame and FFmpeg
                        frame_rgb_cont = numpy.ascontiguousarray(frame_bgr[..., ::-1]) 
                        
                        self.current_frame = frame_rgb_cont # Store RGB for FFmpeg/processing
                        # Create pixmap *from the BGR contiguous frame* for correct initial display
                        pixmap = common_widget_actions.get_pixmap_from_frame(self.main_window, frame_bgr_cont)
                        graphics_view_actions.update_graphics_view(self.main_window, pixmap, start_frame)
                    else:
                        print(f"[WARN] Could not read frame after seeking to {start_frame}. FFmpeg might use wrong dimensions.")
                    # Reset capture to the start frame position, as read_frame advances it
                    self.media_capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
                else:
                    print("process_video: Recording mode - No valid Job Start Frame, using current position.")
                    # Ensure self.current_frame is updated if starting from non-zero without seek
                    if self.current_frame_number != 0 and len(self.current_frame) == 0:
                         ret, frame = misc_helpers.read_frame(self.media_capture, preview_mode=False)
                         if ret: self.current_frame = frame[..., ::-1]
                         self.media_capture.set(cv2.CAP_PROP_POS_FRAMES, self.current_frame_number) # Go back

                # 2. Calculate play_start_time *after* potential seek
                self.play_start_time = float(self.media_capture.get(cv2.CAP_PROP_POS_FRAMES) / float(self.fps))

                # 3. Create FFmpeg subprocess *after* seek and potential frame read
                if len(self.current_frame) > 0: # Ensure we have a frame for dimensions
                    self.create_ffmpeg_subprocess()
                else:
                    print("[ERROR] Cannot start FFmpeg subprocess: No current frame data available for dimensions.")
                    # Handle error - maybe stop processing?
                    self.stop_processing()
                    return
            else:
                # Not recording, calculate start time normally
                self.play_start_time = float(self.media_capture.get(cv2.CAP_PROP_POS_FRAMES) / float(self.fps))
            # --- End Recording Setup ---

            # --- Start Timers ---
            self.frame_display_timer.timeout.connect(self.display_next_frame)
            self.frame_read_timer.timeout.connect(self.process_next_frame)

            if self.main_window.control['VideoPlaybackCustomFpsToggle']:
                fps = self.main_window.control['VideoPlaybackCustomFpsSlider']
            else:
                fps = self.media_capture.get(cv2.CAP_PROP_FPS)
            
            interval = 1000 / fps if fps > 0 else 30
            interval = int(interval * 0.8) #Process 20% faster to offset the frame loading & processing time so the video will be played close to the original fps
            print(f"Starting frame_read_timer with an interval of {interval} ms.")
            if self.recording:
                self.frame_read_timer.start()
                self.frame_display_timer.start()
            else:
                self.frame_read_timer.start(interval)
                self.frame_display_timer.start()
            self.gpu_memory_update_timer.start(5000) #Update GPU memory progressbar every 5 Seconds

        elif self.file_type == 'webcam':
            print("Calling process_video() on Webcam stream")
            self.processing = True
            self.frames_to_display.clear()
            self.threads.clear()
            fps = self.media_capture.get(cv2.CAP_PROP_FPS)
            interval = 1000 / fps if fps > 0 else 30
            interval = int(interval * 0.8) #Process 20% faster to offset the frame loading & processing time so the video will be played close to the original fps
            self.frame_read_timer.timeout.connect(self.process_next_webcam_frame)
            self.frame_read_timer.start(interval)
            self.frame_display_timer.timeout.connect(self.display_next_webcam_frame)
            self.frame_display_timer.start()
            self.gpu_memory_update_timer.start(5000) #Update GPU memory progressbar every 5 Seconds

    def process_next_frame(self):
        """Read the next frame and add it to the queue for processing."""
        if self.current_frame_number > self.max_frame_number:
            # print("Stopping frame_read_timer as all frames have been read!")
            self.frame_read_timer.stop()
            return

        if self.frame_queue.qsize() >= self.num_threads:
            # print(f"Queue is full ({self.frame_queue.qsize()} frames). Throttling frame reading.")
            return

        if self.file_type == 'video' and self.media_capture:
            ret, frame = misc_helpers.read_frame(self.media_capture, preview_mode = not self.recording)
            if ret:
                frame = frame[..., ::-1]  # Convert BGR to RGB
                self.frame_queue.put(self.current_frame_number)
                self.start_frame_worker(self.current_frame_number, frame)
                self.current_frame_number += 1
            else:
                print("Cannot read frame!", self.current_frame_number)
                self.stop_processing()
                self.main_window.display_messagebox_signal.emit('Error Reading Frame', f'Error Reading Frame {self.current_frame_number}.\n Stopped Processing...!', self.main_window)

    def start_frame_worker(self, frame_number, frame, is_single_frame=False):
        """Start a FrameWorker to process the given frame."""
        worker = FrameWorker(frame, self.main_window, frame_number, self.frame_queue, is_single_frame)
        self.threads[frame_number] = worker
        if is_single_frame:
            worker.run()
        else:
            worker.start()

    def process_current_frame(self):

        # print("\nCalled process_current_frame()",self.current_frame_number)
        # self.main_window.processed_frames.clear()

        self.next_frame_to_display = self.current_frame_number
        if self.file_type == 'video' and self.media_capture:
            ret, frame = misc_helpers.read_frame(self.media_capture, preview_mode=False)
            if ret:
                frame = frame[..., ::-1]  # Convert BGR to RGB
                # print(f"Enqueuing frame {self.current_frame_number}")
                self.frame_queue.put(self.current_frame_number)
                self.start_frame_worker(self.current_frame_number, frame, is_single_frame=True)
                
                self.media_capture.set(cv2.CAP_PROP_POS_FRAMES, self.current_frame_number)
            else:
                print("Cannot read frame!", self.current_frame_number)
                self.main_window.display_messagebox_signal.emit('Error Reading Frame', f'Error Reading Frame {self.current_frame_number}.', self.main_window)

        # """Process a single image frame directly without queuing."""
        elif self.file_type == 'image':
            frame = misc_helpers.read_image_file(self.media_path)
            if frame is not None:

                frame = frame[..., ::-1]  # Convert BGR to RGB
                self.frame_queue.put(self.current_frame_number)
                # print("Processing current frame as image.")
                self.start_frame_worker(self.current_frame_number, frame, is_single_frame=True)
            else:
                print("Error: Unable to read image file.")

        # Handle webcam capture
        elif self.file_type == 'webcam':
            ret, frame = misc_helpers.read_frame(self.media_capture, preview_mode = False)
            if ret:
                frame = frame[..., ::-1]  # Convert BGR to RGB
                # print(f"Enqueuing frame {self.current_frame_number}")
                self.frame_queue.put(self.current_frame_number)
                self.start_frame_worker(self.current_frame_number, frame, is_single_frame=True)
            else:
                print("Unable to read Webcam frame!")
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
    def stop_processing(self, stopped_by_end_frame=False):
        """Stop video processing and signal completion."""
        if not self.processing:
            # print("Processing not active. No action to perform.")
            video_control_actions.reset_media_buttons(self.main_window)

            return False
        
        print("Stopping video processing.")
        self.processing = False
        
        if self.file_type=='video' or self.file_type=='webcam':

            # print("Stopping Timers")
            self.frame_read_timer.stop()
            self.frame_display_timer.stop()
            self.gpu_memory_update_timer.stop()
            self.join_and_clear_threads()


            # print("Clearing Threads and Queues")
            self.threads.clear()
            self.frames_to_display.clear()
            self.webcam_frames_to_display.queue.clear()

            with self.frame_queue.mutex:
                self.frame_queue.queue.clear()

            self.current_frame_number = self.main_window.videoSeekSlider.value()
            self.media_capture.set(cv2.CAP_PROP_POS_FRAMES, self.current_frame_number)

            if self.recording and self.file_type=='video':
                # Check if subprocess exists before trying to close stdin
                if self.recording_sp and self.recording_sp.stdin:
                    try:
                        self.recording_sp.stdin.close()
                        self.recording_sp.wait()
                    except OSError as e:
                        # Handle potential errors if the pipe is already closed or broken
                        print(f"[WARN] Error closing/waiting for FFmpeg stdin: {e}")
                else:
                    print("[WARN] FFmpeg subprocess not available or stdin already closed during stop_processing.")

            # --- Calculate end time ---
            if stopped_by_end_frame and self.main_window.job_end_frame is not None:
                # Use the specified end frame for calculation if stopped by end frame check
                end_frame_num_for_calc = self.main_window.job_end_frame
            else:
                # Fallback: Use current slider position (might be inaccurate if lagging)
                # Update internal number based on slider before calculation
                self.current_frame_number = self.main_window.videoSeekSlider.value() 
                end_frame_num_for_calc = self.current_frame_number 

            # Ensure end frame is at least one frame after start frame number for ffmpeg -ss/-to validity
            start_frame_num = int(self.play_start_time * self.fps) if self.fps > 0 else 0
            end_frame_num_for_calc = max(end_frame_num_for_calc, start_frame_num + 1)
            
            self.play_end_time = float(end_frame_num_for_calc / float(self.fps)) if self.fps > 0 else self.play_start_time + 0.1 # Avoid division by zero
            # --- End Calculate end time ---

            if self.file_type=='video':
                if self.recording:
                    # Use job name for output if specified in the loaded job
                    job_name = getattr(self.main_window, 'current_job_name', None)
                    use_job_name_for_output = getattr(self.main_window, 'use_job_name_for_output', False)
                    final_file_path = misc_helpers.get_output_file_path(
                        self.media_path,
                        self.main_window.control['OutputMediaFolder'],
                        job_name=job_name,
                        use_job_name_for_output=use_job_name_for_output
                    )
                    if Path(final_file_path).is_file():
                        os.remove(final_file_path)
                    
                    # --- FFmpeg Final Merge --- 
                    # Ensure end time is strictly greater than start time before attempting merge
                    if self.play_end_time > self.play_start_time:
                        print(f"Adding audio... Start: {self.play_start_time:.3f}s, End: {self.play_end_time:.3f}s")
                        args = ["ffmpeg",
                                '-hide_banner',
                                '-loglevel',    'error',
                                "-i", self.temp_file, # Input video (processed frames)
                                "-ss", str(self.play_start_time), "-to", str(self.play_end_time), "-i",  self.media_path, # Input audio segment from original
                                "-c",  "copy", # Copy video codec, copy audio codec
                                "-map", "0:v:0", "-map", "1:a:0?", # Map video from input 0, audio from input 1 (optional)
                                "-shortest", # Finish encoding when the shortest input stream ends
                                final_file_path]
                        try:
                            subprocess.run(args, check=True) # Add Audio, check=True to raise error if ffmpeg fails
                        except subprocess.CalledProcessError as e:
                            print(f"[ERROR] FFmpeg audio merge failed: {e}")
                            # Keep temp file for debugging? Or delete?
                        except FileNotFoundError:
                            print(f"[ERROR] FFmpeg not found. Ensure it's in your system PATH.")
                        finally:
                             # Clean up temp file only if it exists
                            if os.path.exists(self.temp_file):
                                os.remove(self.temp_file)
                    else:
                        print(f"[WARN] Skipping audio merge: End time ({self.play_end_time:.3f}s) is not after start time ({self.play_start_time:.3f}s). Final file might be missing audio or be empty.")
                         # Clean up temp file if it exists, as merge was skipped
                        if os.path.exists(self.temp_file):
                            os.remove(self.temp_file)
                    # --- End FFmpeg Final Merge --- 

                self.end_time = time.perf_counter()
                processing_time = self.end_time - self.start_time
                print(f"\nProcessing completed in {processing_time} seconds")
                avg_fps = ((self.play_end_time - self.play_start_time) * self.fps) / processing_time
                print(f'Average FPS: {avg_fps}\n')

                if self.recording:
                    layout_actions.enable_all_parameters_and_control_widget(self.main_window)

            self.recording = False #Set recording as False to make sure the next process_video() call doesnt not record the video, unless the user press the record button

            print("Clearing Cache")
            torch.cuda.empty_cache()
            gc.collect()
            video_control_actions.reset_media_buttons(self.main_window)
            print("Successfully Stopped Processing")
            return True
        
    def join_and_clear_threads(self):
        # print("Joining Threads")
        for _, thread in self.threads.items():
            if thread.is_alive():
                thread.join()
        # print('Clearing Threads')
        self.threads.clear()
    
    def create_ffmpeg_subprocess(self):
        # Use Dimensions of the last processed frame as it could be different from the original frame due to restorers and frame enhancers 
        frame_height, frame_width, _ = self.current_frame.shape

        self.temp_file = r'temp_output.mp4'
        if Path(self.temp_file).is_file():
            os.remove(self.temp_file)

        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "rawvideo",             # Specify raw video input
            "-pix_fmt", "bgr24",          # Pixel format of input frames
            "-s", f"{frame_width}x{frame_height}",  # Frame resolution
            "-r", str(self.fps),          # Frame rate
            "-i", "pipe:",                # Input from stdin
            "-vf", f"pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuvj420p",  # Padding and format conversion            
            "-c:v", "libx264",            # H.264 codec
            "-crf", "18",                 # Quality setting
            self.temp_file                # Output file
        ]

        self.recording_sp = subprocess.Popen(args, stdin=subprocess.PIPE)

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
                print(e)

    def disable_virtualcam(self):
        if self.virtcam:
            self.virtcam.close()
        self.virtcam = None