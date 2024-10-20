import os
import shutil
import subprocess
import tempfile
import time

class VLCPlayer:

    def __init__(self, config):
        self._process = None
        self._temp_directory = None
        self._load_config(config)

    def __del__(self):
        if self._temp_directory:
            shutil.rmtree(self._temp_directory)

    def _get_temp_directory(self):
        if not self._temp_directory:
            self._temp_directory = tempfile.mkdtemp()
        return self._temp_directory

    def _load_config(self, config):
        self._vlc_args = config.get('omxplayer', 'extra_args').split()
        self._show_titles = config.getboolean('omxplayer', 'show_titles')
        if self._show_titles:
            title_duration = config.getint('omxplayer', 'title_duration')
            self._subtitle_header = f"{title_duration}\n"  # VLC subtitle header

    def supported_extensions(self):
        return ['.mp4', '.avi', '.mkv', '.mov']  # Add more extensions as needed

    def play(self, movie, loop=None, vol=0):
        self.stop(3)  # Up to 3-second delay to stop the old player.

        # Assemble the list of arguments for VLC.
        args = ['vlc']
        args.extend(self._vlc_args)  # Add VLC arguments from config.

        if loop is None:
            loop = movie.repeats
        if loop <= -1:
            args.append('--loop')  # Add loop parameter if necessary.

        if self._show_titles and movie.title:
            srt_path = os.path.join(self._get_temp_directory(), 'video_looper.srt')
            with open(srt_path, 'w') as f:
                f.write(self._subtitle_header)
                f.write(movie.title)
            args.extend(['--sub-file', srt_path])
        args.append(movie.target)  # Add movie file path.
        # Run VLC process and direct standard output to /dev/null.
        # Establish input pipe for commands
        self._process = subprocess.Popen(args, stdout=open(os.devnull, 'wb'), close_fds=True)

    def pause(self):
        self.sendKey("p")
    
    def sendKey(self, key: str):
        if self.is_playing():
            self._process.stdin.write(key.encode())
            self._process.stdin.flush()

    def is_playing(self):
        if self._process is None:
            return False
        self._process.poll()
        return self._process.returncode is None

    def stop(self, block_timeout_sec=0):
        # Stop the VLC player if it's running.
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            self._process.wait()
        # If a blocking timeout was specified, wait up to that amount of time
        # for the process to stop.
        start = time.time()
        while self._process is not None and self._process.returncode is None:
            if (time.time() - start) >= block_timeout_sec:
                break
            time.sleep(0)
        # Let the process be garbage collected.
        self._process = None

    @staticmethod
    def can_loop_count():
        return False

def create_player(config, **kwargs):
    return VLCPlayer(config)
