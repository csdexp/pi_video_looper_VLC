# Copyright 2015 Adafruit Industries.
# Author: Tony DiCola
# License: GNU GPLv2, see LICENSE.txt

import configparser
import importlib
import os
import re
import subprocess
import sys
import signal
import time
import pygame
import json
import threading
from datetime import datetime
import RPi.GPIO as GPIO

from .alsa_config import parse_hw_device
from .model import Playlist, Movie
from .playlist_builders import build_playlist_m3u
from datetime import datetime
import vlc

# Basic video looper architecure:
#
# - VideoLooper class contains all the main logic for running the looper program.
#
# - Almost all state is configured in a .ini config file which is required for
#   loading and using the VideoLooper class.
#
# - VideoLooper has loose coupling with file reader and video player classes that
#   are used to find movie files and play videos respectively.  The configuration
#   defines which file reader and video player module will be loaded.
#
# - A file reader module needs to define at top level create_file_reader function
#   that takes as a parameter a ConfigParser config object.  The function should
#   return an instance of a file reader class.  See usb_drive.py and directory.py
#   for the two provided file readers and their public interface.
#
# - Similarly a video player modules needs to define a top level create_player
#   function that takes in configuration.  See omxplayer.py and hello_video.py
#   for the two provided video players and their public interface.
#
# - Future file readers and video players can be provided and referenced in the
#   config to extend the video player use to read from different file sources
#   or use different video players.
class VideoLooper:

    def __init__(self, config_path):
        """Create an instance of the main video looper application class. Must
        pass path to a valid video looper ini configuration file.
        """
        # Load the configuration.
        self._config = configparser.ConfigParser()
        if len(self._config.read(config_path)) == 0:
            raise RuntimeError('Failed to find configuration file at {0}, is the application properly installed?'.format(config_path))
        self._console_output = self._config.getboolean('video_looper', 'console_output')
        # Load other configuration values.
        self._osd = self._config.getboolean('video_looper', 'osd')
        self._is_random = self._config.getboolean('video_looper', 'is_random')
        self._one_shot_playback = self._config.getboolean('video_looper', 'one_shot_playback')
        self._play_on_startup = self._config.getboolean('video_looper', 'play_on_startup')
        self._resume_playlist = self._config.getboolean('video_looper', 'resume_playlist')
        self._keyboard_control = self._config.getboolean('control', 'keyboard_control')
        self._keyboard_control_disabled_while_playback = self._config.getboolean('control', 'keyboard_control_disabled_while_playback')
        self._gpio_control_disabled_while_playback = self._config.getboolean('control', 'gpio_control_disabled_while_playback')
        self._copyloader = self._config.getboolean('copymode', 'copyloader')
        # Get seconds for countdown from config
        self._countdown_time = self._config.getint('video_looper', 'countdown_time')
        # Get seconds for wait time between files from config
        self._wait_time = self._config.getint('video_looper', 'wait_time')
        # Get time display settings
        self._datetime_display = self._config.getboolean('video_looper', 'datetime_display')
        self._top_datetime_display_format = self._config.get('video_looper', 'top_datetime_display_format', raw=True)
        self._bottom_datetime_display_format = self._config.get('video_looper', 'bottom_datetime_display_format', raw=True)
        # Parse string of 3 comma separated values like "255, 255, 255" into
        # a list of ints for colors.
        self._bgcolor = list(map(int, self._config.get('video_looper', 'bgcolor')
                                             .translate(str.maketrans('','', ','))
                                             .split()))
        self._fgcolor = list(map(int, self._config.get('video_looper', 'fgcolor')
                                             .translate(str.maketrans('','', ','))
                                             .split()))
        # Initialize pygame and display a blank screen.
        pygame.display.init()
        pygame.font.init()
        pygame.mouse.set_visible(False)
        self._screen = pygame.display.set_mode((0,0), pygame.FULLSCREEN | pygame.NOFRAME)
        self._size = (pygame.display.Info().current_w, pygame.display.Info().current_h)
        self._bgimage = self._load_bgimage()  # A tuple with pyimage, xpos, ypos
        self._blank_screen()
        # Load configured video player and file reader modules.
        self._player = self._load_player()
        self._reader = self._load_file_reader()
        self._playlist = None
        # Load ALSA hardware configuration.
        self._alsa_hw_device = parse_hw_device(self._config.get('alsa', 'hw_device'))
        self._alsa_hw_vol_control = self._config.get('alsa', 'hw_vol_control')
        self._alsa_hw_vol_file = self._config.get('alsa', 'hw_vol_file')
        # Default ALSA hardware volume (volume will not be changed)
        self._alsa_hw_vol = None
        # Load sound volume file name value
        self._sound_vol_file = self._config.get('omxplayer', 'sound_vol_file')
        # Default value to 0 millibels (omxplayer)
        self._sound_vol = 0
        # Set other static internal state.
        self._extensions = '|'.join(self._player.supported_extensions())
        self._small_font = pygame.font.Font(None, 50)
        self._medium_font   = pygame.font.Font(None, 96)
        self._big_font   = pygame.font.Font(None, 250)
        self._running    = True
        # set the inital playback state according to the startup setting.
        self._playbackStopped = not self._play_on_startup
        # Used for not waiting the first time
        self._firstStart = True

        # Start keyboard handler thread:
        # Event handling for key press, if keyboard control is enabled
        if self._keyboard_control:
            self._keyboard_thread = threading.Thread(target=self._handle_keyboard_shortcuts, daemon=True)
            self._keyboard_thread.start()
        
        pinMapSetting = self._config.get('control', 'gpio_pin_map', raw=True)
        if pinMapSetting:
            try:
                self._pinMap = json.loads("{"+pinMapSetting+"}")
                self._gpio_setup()
            except Exception as err:
                self._pinMap = None
                self._print("gpio_pin_map setting is not valid and/or error with GPIO setup")
        else:
            self._pinMap = None

    def _print(self, message):
        """Print message to standard output if console output is enabled."""
        if self._console_output:
            now = datetime.now()
            print("[{}] {}".format(now, message))

    def _load_player(self):
        """Load the configured video player and return an instance of it."""
        module = self._config.get('video_looper', 'video_player')
        return importlib.import_module('.' + module, 'Adafruit_Video_Looper').create_player(self._config, screen=self._screen, bgimage=self._bgimage)
        # Load VLCPlayer instead of OMXPlayer
        #from .omxplayer import VLCPlayer
        #return VLCPlayer(self._config, self._screen, self._bgimage)

    def _load_file_reader(self):
        """Load the configured file reader and return an instance of it."""
        module = self._config.get('video_looper', 'file_reader')
        return importlib.import_module('.' + module, 'Adafruit_Video_Looper').create_file_reader(self._config, self._screen)

    def _load_bgimage(self):
        """Load the configured background image and return an instance of it."""
        image = None
        image_x = 0
        image_y = 0

        if self._config.has_option('video_looper', 'bgimage'):
            imagepath = self._config.get('video_looper', 'bgimage')
            if imagepath != "" and os.path.isfile(imagepath):
                self._print('Using ' + str(imagepath) + ' as a background')
                image = pygame.image.load(imagepath)

                screen_w, screen_h = self._size
                image_w, image_h = image.get_size()

                screen_aspect_ratio = screen_w / screen_h
                photo_aspect_ratio = image_w / image_h

                if screen_aspect_ratio < photo_aspect_ratio:  # Width is binding
                    new_image_w = screen_w
                    new_image_h = int(new_image_w / photo_aspect_ratio)
                    image = pygame.transform.scale(image, (new_image_w, new_image_h))
                    image_y = (screen_h - new_image_h) // 2

                elif screen_aspect_ratio > photo_aspect_ratio:  # Height is binding
                    new_image_h = screen_h
                    new_image_w = int(new_image_h * photo_aspect_ratio)
                    image = pygame.transform.scale(image, (new_image_w, new_image_h))
                    image_x = (screen_w - new_image_w) // 2

                else:  # Images have the same aspect ratio
                    image = pygame.transform.scale(image, (screen_w, screen_h))

        return (image, image_x, image_y)

    def _is_number(self, s):
        try:
            float(s)
            return True
        except ValueError:
            return False

    def _build_playlist(self):
        """Try to build a playlist (object) from a playlist (file).
        Falls back to an auto-generated playlist with all files.
        """
        if self._config.has_option('playlist', 'path'):
            playlist_path = self._config.get('playlist', 'path')
            if playlist_path != "":
                if os.path.isabs(playlist_path):
                    if not os.path.isfile(playlist_path):
                        self._print('Playlist path {0} does not exist.'.format(playlist_path))
                        return self._build_playlist_from_all_files()
                else:
                    paths = self._reader.search_paths()

                    if not paths:
                        return Playlist([])

                    for path in paths:
                        maybe_playlist_path = os.path.join(path, playlist_path)
                        if os.path.isfile(maybe_playlist_path):
                            playlist_path = maybe_playlist_path
                            self._print('Playlist path resolved to {0}.'.format(playlist_path))
                            break
                    else:
                        self._print('Playlist path {0} does not resolve to any file.'.format(playlist_path))
                        return self._build_playlist_from_all_files()

                basepath, extension = os.path.splitext(playlist_path)
                if extension == '.m3u' or extension == '.m3u8':
                    return build_playlist_m3u(playlist_path)
                else:
                    self._print('Unrecognized playlist format {0}.'.format(extension))
                    return self._build_playlist_from_all_files()
            else:
                return self._build_playlist_from_all_files()
        else:
            return self._build_playlist_from_all_files()

    def _build_playlist_from_all_files(self):
        """Search all the file reader paths for movie files with the provided
        extensions.
        """
        # Get list of paths to search from the file reader.
        paths = self._reader.search_paths()
        # Enumerate all movie files inside those paths.
        movies = []
        for path in paths:
            # Skip paths that don't exist or are files.
            if not os.path.exists(path) or not os.path.isdir(path):
                continue

            for x in os.listdir(path):
                # Ignore hidden files (useful when file loaded on USB key from an OSX computer)
                if x[0] != '.' and re.search('\.({0})$'.format(self._extensions), x, flags=re.IGNORECASE):
                    repeatsetting = re.search('_repeat_([0-9]*)x', x, flags=re.IGNORECASE)
                    if (repeatsetting is not None):
                        repeat = repeatsetting.group(1)
                    else:
                        repeat = 1
                    basename, extension = os.path.splitext(x)
                    movies.append(Movie('{0}/{1}'.format(path.rstrip('/'), x), basename, repeat))

            # Get the ALSA hardware volume from the file in the USB key
            if self._alsa_hw_vol_file:
                alsa_hw_vol_file_path = '{0}/{1}'.format(path.rstrip('/'), self._alsa_hw_vol_file)
                if os.path.exists(alsa_hw_vol_file_path):
                    with open(alsa_hw_vol_file_path, 'r') as alsa_hw_vol_file:
                        alsa_hw_vol_string = alsa_hw_vol_file.readline()
                        self._alsa_hw_vol = alsa_hw_vol_string

            # Get the video volume from the file in the USB key
            if self._sound_vol_file:
                sound_vol_file_path = '{0}/{1}'.format(path.rstrip('/'), self._sound_vol_file)
                if os.path.exists(sound_vol_file_path):
                    with open(sound_vol_file_path, 'r') as sound_file:
                        sound_vol_string = sound_file.readline()
                        if self._is_number(sound_vol_string):
                            self._sound_vol = int(float(sound_vol_string))
        # Create a playlist with the sorted list of movies.
        return Playlist(sorted(movies))

    def _blank_screen(self):
        """Render a blank screen filled with the background color and optional the background image."""
        self._screen.fill(self._bgcolor)
        if self._bgimage[0] is not None:
            self._screen.blit(self._bgimage[0], (self._bgimage[1], self._bgimage[2]))
        pygame.display.flip()

    def _render_text(self, message, font=None):
        """Draw the provided message and return as pygame surface of it rendered
        with the configured foreground and background color.
        """
        # Default to small font if not provided.
        if font is None:
            font = self._small_font
        return font.render(message, True, self._fgcolor, self._bgcolor)

    def _animate_countdown(self, playlist):
        """Print text with the number of loaded movies and a quick countdown
        message if the on-screen display is enabled.
        """
        # Print message to console with the number of media files in playlist.
        message = 'Found {0} media file{1}.'.format(playlist.length(),
                                                    's' if playlist.length() >= 2 else '')
        self._print(message)
        # Do nothing else if the OSD is turned off.
        if not self._osd:
            return
        # Draw message with the number of movies loaded and animate countdown.
        # First render text that doesn't change and get static dimensions.
        label1 = self._render_text(message + ' Starting playback in:')
        l1w, l1h = label1.get_size()
        # Static X position for label 1.
        l1x = (self._size[0] - l1w) // 2
        # Get static height position for both labels.
        y = (self._size[1] - l1h) // 2
        # Create a countdown animation for each second in the countdown.
        for i in range(self._countdown_time, 0, -1):
            # Blank screen.
            self._blank_screen()
            # Render the first label with the number of movies.
            self._screen.blit(label1, (l1x, y))
            # Render the second label with the countdown.
            label2 = self._render_text(str(i), font=self._big_font)
            l2w, l2h = label2.get_size()
            # Static X position for label 2.
            l2x = (self._size[0] - l2w) // 2
            # Render the second label with countdown.
            self._screen.blit(label2, (l2x, y + l1h))
            # Update display.
            pygame.display.flip()
            # Wait for 1 second.
            time.sleep(1)
        # Blank screen and continue.
        self._blank_screen()

    def _handle_keyboard_shortcuts(self):
        """Keyboard handler thread to listen for shortcuts and handle them."""
        # Register a handler for the control-c signal to cleanly exit the thread.
        signal.signal(signal.SIGINT, self._handle_exit_signal)
        while self._running:
            for event in pygame.event.get():
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        # Exit the application when the 'Esc' key is pressed.
                        self._running = False
                    elif event.key == pygame.K_SPACE:
                        # Pause or unpause playback when the 'Space' key is pressed.
                        self._player.toggle_pause()
                    elif event.key == pygame.K_RIGHT:
                        # Play next video when the right arrow key is pressed.
                        self._play_next()
                    elif event.key == pygame.K_LEFT:
                        # Play previous video when the left arrow key is pressed.
                        self._play_previous()
                    elif event.key == pygame.K_r:
                        # Reload the playlist when the 'r' key is pressed.
                        self._playlist = self._build_playlist()
                        self._play_next()
                    elif event.key == pygame.K_s:
                        # Stop playback when the 's' key is pressed.
                        self._player.stop()
                    elif event.key == pygame.K_MINUS:
                        # Decrease volume when the '-' key is pressed.
                        self._player.decrease_volume()
                    elif event.key == pygame.K_PLUS or event.key == pygame.K_KP_PLUS:
                        # Increase volume when the '+' key or numeric '+' key is pressed.
                        self._player.increase_volume()
                    elif event.key == pygame.K_m:
                        # Toggle mute when the 'm' key is pressed.
                        self._player.toggle_mute()
                    elif event.key == pygame.K_UP:
                        # Go to the next playlist entry when the 'Up' key is pressed.
                        self._play_next()
                    elif event.key == pygame.K_DOWN:
                        # Go to the previous playlist entry when the 'Down' key is pressed.
                        self._play_previous()
            # Sleep briefly to avoid hogging the CPU.
            time.sleep(0.1)

    def _handle_exit_signal(self, signal, frame):
        """Handler for the control-c signal to cleanly exit the keyboard thread."""
        self._running = False

    def _play_next(self):
        """Play the next video in the playlist."""
        if self._playlist.length() == 0:
            # If there are no videos in the playlist, do nothing.
            return
        # Get the next movie to play.
        movie = self._playlist.next_movie(random=self._is_random)
        self._print('Playing next video: {0}'.format(movie.path))
        self._player.play(movie.path, self._sound_vol)

    def _play_previous(self):
        """Play the previous video in the playlist."""
        if self._playlist.length() == 0:
            # If there are no videos in the playlist, do nothing.
            return
        # Get the previous movie to play.
        movie = self._playlist.previous_movie(random=self._is_random)
        self._print('Playing previous video: {0}'.format(movie.path))
        self._player.play(movie.path, self._sound_vol)

    def _handle_gpio_control(self, pin):
        if self._pinMap == None:
            return
        
        if self._gpio_control_disabled_while_playback and self._player.is_playing():
            self._print(f'gpio control disabled while playback is running')
            return
        
        action = self._pinMap[str(pin)]

        self._print(f'pin {pin} triggered: {action}')
        
        if action in ['K_ESCAPE', 'K_k', 'K_s', 'K_SPACE', 'K_p', 'K_b', 'K_o', 'K_i']:
            pygame.event.post(pygame.event.Event(pygame.KEYDOWN, key=getattr(pygame, action, None)))
        else:
            self._playlist.set_next(action)
            self._player.stop(3)
            self._playbackStopped = False
    
    def _gpio_setup(self):
        if self._pinMap == None:
            return
        GPIO.setmode(GPIO.BOARD)
        for pin in self._pinMap:
            GPIO.setup(int(pin), GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(int(pin), GPIO.FALLING, callback=self._handle_gpio_control,  bouncetime=200) 
            self._print("pin {} action set to: {}".format(pin, self._pinMap[pin]))
        
    def run(self):
        """Run the video looper application."""
        # Start by creating a playlist.
        self._playlist = self._build_playlist()
        # Initialize the playlist and start playback.
        self._player.initialize()
        if self._resume_playlist:
            self._player.play_last()
        else:
            self._play_next()
        # Animate the countdown.
        if self._countdown_time > 0:
            self._animate_countdown(self._playlist)
        # Main loop to watch for changes and play videos.
        while self._running:
            if not self._playbackStopped:
                # Check if the current video has finished playing.
                if self._player.finished():
                    self._play_next()
                # Check for any changes to the file system.
                new_playlist = self._build_playlist()
                if new_playlist != self._playlist:
                    # If the new playlist is different from the old one, update it.
                    self._playlist = new_playlist
                    self._print('Updating playlist...')
                    self._play_next()  # Start playing the new playlist from the beginning.
                time.sleep(self._wait_time)
            else:
                time.sleep(1)  # Wait for 1 second if playback is stopped.

        # Clean up and exit.
        self._player.stop()
        if self._pinMap:
            GPIO.cleanup()
            
        pygame.quit()

if __name__ == '__main__':
    # Check for configuration file as first argument, otherwise use default.
    config_path = sys.argv[1] if len(sys.argv) > 1 else '/boot/video_looper.ini'
    # Create an instance of the video looper and run it.
    video_looper = VideoLooper(config_path)
    video_looper.run()
