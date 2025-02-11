import asyncio
import threading
import concurrent.futures
import logging
import os
import platform
import random
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from tempfile import NamedTemporaryFile
from typing import List, Optional, Tuple, Generator
from huggingface_hub import snapshot_download
import numpy as np
import torch
from edge_tts import Communicate, SubMaker, VoicesManager
from pydub import AudioSegment
from scipy.io.wavfile import write
from tqdm import tqdm
from txtsplit import txtsplit
from database.crud import (
    ArticleData,
    PodcastData,
    TextData,
    update_article,
    update_podcast,
    update_text,
)
from llm.LLM_calls import generate_title
from TTS.F5_TTS.F5 import get_available_voices as f5_get_voices
from TTS.F5_TTS.F5 import infer, load_transcript
from utils.common_utils import (
    add_mp3_tags,
    get_output_files,
    write_markdown_file,
)
from utils.env import setup_env
from scipy.io import wavfile
import queue
import soundfile as sf
from TTS.fish_speech.tools.llama.generate import (
    GenerateRequest,
    GenerateResponse,
    WrappedGenerateResponse,
)
from TTS.fish_speech.tools.api import encode_reference, decode_vq_tokens
from TTS.fish_speech.tools.llama.generate import generate_long
from TTS.fish_speech.tools.vqgan.inference import (
    load_model as load_decoder_model,
)

logger = logging.getLogger(__name__)

output_dir, task_file, img_pth, sources_file = setup_env()


class TTSEngine(ABC):
    @abstractmethod
    async def generate_audio(
        self, text: str, voice_id: str
    ) -> Tuple[AudioSegment, Optional[str]]:
        """
        Generate audio for given text using specified voice.
        Returns tuple of (AudioSegment, optional VTT file path)
        """
        pass

    @abstractmethod
    async def get_available_voices(self) -> List[str]:
        """Return list of available voice IDs"""
        pass

    async def pick_random_voice(
        self, available_voices: List[str], previous_voice: Optional[str] = None
    ) -> str:
        """
        Picks a random voice from the list of available voices, ensuring it is different from the previously picked voice.

        Args:
            available_voices (list): A list of available voice names.
            previous_voice (str, optional): The voice that was previously picked, if any.

        Returns:
            str: The randomly picked voice.
        """
        if not available_voices:
            raise ValueError("No available voices to select from.")

        if previous_voice and previous_voice in available_voices:
            # Filter out the previous voice from the available choices
            voices_to_choose_from = [
                voice for voice in available_voices if voice != previous_voice
            ]
        else:
            voices_to_choose_from = available_voices

        if not voices_to_choose_from:
            raise ValueError("Only one voice available, cannot pick a different one.")

        # Pick a random voice from the remaining choices
        return random.choice(voices_to_choose_from)

    async def export_audio(
        self,
        audio: AudioSegment,
        text: str,
        title: Optional[str] = None,
        vtt_temp_file: Optional[str] = None,
        audio_type: Optional[str] = None,
        article_id: Optional[str] = None,
        text_id: Optional[int] = None,
        podcast_id: Optional[int] = None,
    ) -> str:
        """Export audio, convert to MP3 if needed, and add metadata"""
        try:
            # Generate title if not provided
            if not title:
                title = generate_title(text)
            base_file_name, output_path, md_file_name = await get_output_files(
                output_dir, title
            )

            # Convert to MP3 if the format is not already MP3
            if not output_path.endswith(".mp3"):
                mp3_file = f"{base_file_name}.mp3"
                audio.export(mp3_file, format="mp3")
                add_mp3_tags(mp3_file, title, img_pth, output_dir, audio_type)
                output_path = mp3_file
            else:
                audio.export(output_path, format="mp3")
                add_mp3_tags(output_path, title, img_pth, output_dir)
            # Write the markdown file for the text
            write_markdown_file(md_file_name, text)
            logger.info(f"Exported audio to {output_path}")

            # Handle optional VTT file if provided
            if vtt_temp_file:
                vtt_file = f"{base_file_name}.vtt"
                shutil.move(vtt_temp_file, vtt_file)

            if article_id:
                if audio_type == "url/full" or "url/tldr":
                    new_article = ArticleData(
                        markdown_file=md_file_name,
                        audio_file=output_path,
                        img_file=img_pth,
                    )
                    update_article(article_id, new_article)
                    logging.info(
                        f"article {article_id} db entry successfully updated with audio data"
                    )

            elif text_id:
                if audio_type == "text/full" or "text/tldr":
                    new_text = TextData(
                        markdown_file=md_file_name,
                        audio_file=output_path,
                        img_file=img_pth,
                    )
                    update_text(text_id, new_text)
                    logging.info(
                        f"Text with {text_id} db entry successfully updated with audio data"
                    )

            elif podcast_id:
                if audio_type == "podcast":
                    new_podcast = PodcastData(
                        audio_file=output_path,
                        img_file=img_pth,
                    )
                    update_podcast(podcast_id, new_podcast)
                    logging.info(
                        f"Podcast {podcast_id} db entry successfully updated with audio data"
                    )

            return output_path
        except Exception as e:
            logger.error(f"Error exporting audio: {e}")
            raise


class EdgeTTSEngine(TTSEngine):
    async def get_available_voices(self) -> List[str]:
        voices = await VoicesManager.create()
        return [
            voice_info["Name"]
            for voice_info in voices.voices
            if "MultilingualNeural" in voice_info["Name"]
            and "en-US" in voice_info["Name"]
        ]

    async def generate_audio(
        self, text: str, voice_id: str
    ) -> Tuple[AudioSegment, Optional[str]]:
        with tempfile.NamedTemporaryFile(suffix=".mp3") as temp_audio:
            with tempfile.NamedTemporaryFile(suffix=".vtt") as temp_vtt:
                # Create communicate object
                communicate = Communicate(text, voice_id)
                submaker = SubMaker()
                # Generate audio and save to temp file
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        temp_audio.write(chunk["data"])
                    elif chunk["type"] == "WordBoundary":
                        # Handle SSML timing data if needed
                        submaker.create_sub(
                            (chunk["offset"], chunk["duration"]), chunk["text"]
                        )
                        with open(temp_vtt.name, "w", encoding="utf-8") as f:
                            f.write(submaker.generate_subs())

                temp_audio.flush()
                audio = AudioSegment.from_file(temp_audio.name)

                # Read VTT content
                with open(temp_vtt.name, "r", encoding="utf-8") as f:
                    vtt_content = f.read()

                # Create permanent VTT file if needed
                vtt_file = None
                if vtt_content.strip():
                    vtt_file = f"{temp_audio.name}.vtt"
                    with open(vtt_file, "w", encoding="utf-8") as f:
                        f.write(vtt_content)

                return audio, vtt_file


class F5TTSEngine(TTSEngine):
    def __init__(self, voice_dir: str):
        self.voice_dir = voice_dir
        self.logger = logging.getLogger(__name__)

    async def get_available_voices(self) -> List[str]:
        try:
            voices = f5_get_voices(self.voice_dir)
            self.logger.info(f"Found {len(voices)} voices in {self.voice_dir}")
            return voices
        except Exception as e:
            self.logger.error(f"Error getting available voices: {e}")
            raise

    async def generate_audio(
        self, text: str, voice_id: str
    ) -> Tuple[AudioSegment, None]:
        try:
            audio_path = os.path.join(self.voice_dir, voice_id)
            self.logger.info(f"Generating audio using voice: {audio_path}")
            ref_text = load_transcript(voice_id, self.voice_dir)
            audio, _ = infer(
                audio_path,
                ref_text,
                text,
                model="F5-TTS",
                remove_silence=True,
                speed=1.1,
            )

            sr, audio_data = audio
            if audio_data is None:
                raise ValueError(f"F5-TTS returned None for voice {voice_id}")

            # Convert numpy array to AudioSegment
            # Ensure the array is normalized to [-1, 1]
            audio_data = np.clip(audio_data, -1, 1)

            # Convert to 16-bit PCM
            audio_np_int16 = (audio_data * 32767).astype(np.int16)

            # Create AudioSegment directly from bytes
            audio_segment = AudioSegment(
                data=audio_np_int16.tobytes(),
                sample_width=2,  # 16-bit
                frame_rate=sr,
                channels=1,  # mono
            )

            self.logger.info(
                f"Successfully generated audio segment of length {len(audio_segment)}ms"
            )
            return audio_segment, None

        except Exception as e:
            self.logger.error(f"Error in generate_audio: {e}")
            raise


class PiperTTSEngine(TTSEngine):
    def __init__(self, voices_dir: str):
        self.voices_dir = os.path.join(voices_dir)
        self.logger = logging.getLogger(__name__)

    async def get_available_voices(self) -> List[str]:
        # List available voices based on subfolder names in the voices directory
        if not os.path.exists(self.voices_dir):
            self.logger.error(f"Voices directory '{self.voices_dir}' does not exist.")
            return []

        # Check each subdirectory for .onnx and .json files
        voice_ids = [
            folder
            for folder in os.listdir(self.voices_dir)
            if os.path.isdir(os.path.join(self.voices_dir, folder))
            and any(
                file.endswith(".onnx")
                for file in os.listdir(os.path.join(self.voices_dir, folder))
            )
            and any(
                file.endswith(".json")
                for file in os.listdir(os.path.join(self.voices_dir, folder))
            )
        ]

        self.logger.info(f"Found {len(voice_ids)} voices in {self.voices_dir}")
        return voice_ids

    async def generate_audio(
        self, text: str, voice_id: str
    ) -> Tuple[AudioSegment, None]:
        try:
            # Determine the path to the Piper binary based on the operating system
            script_folder = os.path.dirname(os.path.abspath(__file__))
            operating_system = platform.system()

            if operating_system == "Windows":
                piper_binary = os.path.join(script_folder, "piper_tts", "piper.exe")
            else:
                piper_binary = os.path.join(script_folder, "piper_tts", "piper")

            voice_folder_path = os.path.join(self.voices_dir, voice_id)

            # Verify the voice model files exist
            model_path = next(
                (
                    os.path.join(voice_folder_path, f)
                    for f in os.listdir(voice_folder_path)
                    if f.endswith(".onnx")
                ),
                None,
            )
            json_path = next(
                (
                    os.path.join(voice_folder_path, f)
                    for f in os.listdir(voice_folder_path)
                    if f.endswith(".json")
                ),
                None,
            )

            if not model_path or not json_path:
                self.logger.error(
                    "Required voice files not found in the specified voice folder."
                )
                raise FileNotFoundError("Piper model or JSON file missing")

            # Use a temporary file for the output audio
            with NamedTemporaryFile(suffix=".wav", delete=False) as temp_audio:
                temp_audio_path = temp_audio.name

            # Construct and execute the Piper command
            command = [
                piper_binary,
                "-m",
                model_path,
                "-c",
                json_path,
                "-f",
                temp_audio_path,
                "-s",
                "0",  # Example: using voice index 0 for multi-voice models
                "--length_scale",
                "1.0",  # Set length scale (speed of speech)
            ]

            process = subprocess.run(
                command,
                input=text.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Check if the process completed successfully
            if process.returncode != 0:
                self.logger.error(
                    f"Piper TTS command failed: {process.stderr.decode()}"
                )
                raise RuntimeError("Piper TTS synthesis failed")

            # Load generated audio file into AudioSegment
            audio = AudioSegment.from_file(temp_audio_path, format="wav")
            self.logger.info(f"Generated Piper TTS audio for voice_id {voice_id}")
            return audio, None
        except Exception as e:
            self.logger.error(f"Error generating audio with Piper TTS: {e}")
            raise


class StyleTTS2Engine(TTSEngine):
    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def get_available_voices(self) -> List[str]:
        # Only one voice for StyleTTS2
        return ["styletts2_default_voice"]

    async def generate_audio(
        self, text: str, voice_id: str
    ) -> Tuple[AudioSegment, None]:
        from .styletts2.ljspeechimportable import inference

        try:
            # Ensure the text is valid and within length limits
            if not text.strip():
                self.logger.error("Text input is empty.")
                raise ValueError("Empty text input")

            if len(text) > 150000:
                self.logger.error("Text must be <150k characters")
                raise ValueError("Text is too long")

            # Split the text and synthesize each segment
            texts = txtsplit(text)
            audios = []
            noise = torch.randn(1, 1, 256).to(
                "cuda" if torch.cuda.is_available() else "cpu"
            )

            for t in tqdm(texts, desc="Synthesizing with StyleTTS2"):
                audio_segment = inference(
                    t, noise, diffusion_steps=5, embedding_scale=1
                )
                if audio_segment is not None:
                    audios.append(audio_segment)
                else:
                    self.logger.error(f"Inference returned None for text segment: {t}")

            if not audios:
                raise ValueError("No audio segments were generated")

            # Concatenate all audio segments and save to a temporary WAV file
            full_audio = np.concatenate(audios)
            with NamedTemporaryFile(suffix=".wav", delete=False) as temp_wav_file:
                write(temp_wav_file.name, 24000, full_audio)
                temp_wav_path = temp_wav_file.name

            # Load the temporary WAV file as an AudioSegment
            audio = AudioSegment.from_file(temp_wav_path, format="wav")
            return audio, None
        except Exception as e:
            self.logger.error(f"Error generating audio with StyleTTS2: {e}")


class FishTTSEngine(TTSEngine):
    def __init__(
        self,
        model_repo: str,
        voices_dir: str,
        max_length: int = 2048,
    ):
        """
        Initialize FishTTSEngine with required models and configurations.

        Args:
            model_repo: HuggingFace repository ID for the main model
            voices_dir: Directory containing voice reference files
            device: Computing device (cuda/cpu)
            max_length: Maximum sequence length
        """
        device = "cuda" if torch.cuda.is_available() else "cpu"

        try:
            self.model_repo = model_repo
            self.voices_dir = voices_dir
            self.device = device
            self.max_length = max_length
            self.cache_dir = os.path.join(
                os.path.dirname(__file__), "fish_speech", "checkpoints"
            )
            self.logger = logging.getLogger(__name__)

            # Create cache directory if it doesn't exist
            os.makedirs(self.cache_dir, exist_ok=True)

            # Import required modules here to avoid early import issues
            # from .fish_speech.tools.llama.generate import launch_thread_safe_queue

            self.encode_reference = encode_reference
            self.decode_vq_tokens = decode_vq_tokens

            # Download the entire model repository
            self.repo_path = snapshot_download(
                repo_id=self.model_repo,
                cache_dir=self.cache_dir,
            )

            # Set decoder checkpoint path
            self.decoder_checkpoint_path = os.path.join(
                self.repo_path, "firefly-gan-vq-fsq-8x1024-21hz-generator.pth"
            )

            # Initialize models
            self.llama_queue = self.launch_thread_safe_queue(
                checkpoint_path=self.repo_path,
                device=self.device,
                precision=torch.bfloat16,
                compile=False,
            )
            self.decoder_model = load_decoder_model(
                config_name="firefly_gan_vq",
                checkpoint_path=self.decoder_checkpoint_path,
                device=self.device,
            )

            # Process reference files
            self._prepare_reference_files()

        except Exception as e:
            self.logger.error(f"Error initializing FishTTSEngine: {e}")
            raise

    def _prepare_reference_files(self):
        """Generate .npy reference files from .wav voice samples."""
        try:
            for wav_file in os.listdir(self.voices_dir):
                if wav_file.endswith(".wav"):
                    wav_path = os.path.join(self.voices_dir, wav_file)
                    self.logger.info(f"Processing audio file: {wav_file}")
                    npy_file = os.path.join(
                        self.voices_dir, f"{os.path.splitext(wav_file)[0]}.npy"
                    )
                    if not os.path.exists(npy_file):
                        # Read the audio data
                        try:
                            sr, audio_data = wavfile.read(wav_path)
                        except Exception as e:
                            self.logger.error(f"Error reading {wav_file}: {e}")
                            continue  # Skip to the next file

                        # Check if audio data is not empty
                        if audio_data.size == 0:
                            self.logger.warning(
                                f"Skipping empty audio file: {wav_file}"
                            )
                            continue  # Skip to the next file

                        # Ensure the audio data is in the correct format
                        if audio_data.dtype != np.float32:
                            max_value = np.iinfo(audio_data.dtype).max
                            audio_data = audio_data.astype(np.float32) / max_value

                        # Calculate duration
                        duration = audio_data.shape[0] / sr
                        self.logger.info(f"Audio duration: {duration:.2f} seconds")

                        # Skip short audio files (e.g., less than 1 second)
                        if duration < 1.0:
                            self.logger.warning(
                                f"Skipping short audio file ({duration:.2f}s): {wav_file}"
                            )
                            continue  # Skip to the next file

                        # Encode reference audio
                        encoded_reference = self.encode_reference(
                            decoder_model=self.decoder_model,
                            reference_audio=audio_data,
                            enable_reference_audio=True,
                        )
                        np.save(npy_file, encoded_reference)
                        self.logger.info(f"Generated reference file: {npy_file}")

        except Exception as e:
            self.logger.error(f"Error preparing reference files: {e}")
            raise

    def _load_reference_tokens(self, voice_id: str):
        npy_file = os.path.join(self.voices_dir, f"{voice_id}.npy")
        if os.path.exists(npy_file):
            reference_tokens = np.load(npy_file)
            self.logger.debug(
                f"Loaded reference tokens for voice {voice_id}: {reference_tokens.shape}"
            )
            return reference_tokens
        else:
            self.logger.warning(f"Reference tokens not found for voice {voice_id}")
            return None

    def load_model(
        self,
        checkpoint_path: str,
        device: str,
        precision: torch.dtype,
        compile: bool = False,
    ):
        """
        Loads the model from a checkpoint and configures it for inference.

        Args:
            checkpoint_path (str): Path to the model checkpoint directory.
            device (str): Device to load the model on ('cpu' or 'cuda').
            precision (torch.dtype): Precision to use for model parameters.
            compile (bool): Whether to compile the model with TorchScript (if applicable).

        Returns:
            model: The loaded and configured model.
            decode_one_token: A function for decoding a single token, if applicable.
        """
        # Load the model (example assumes a Hugging Face-style checkpoint)
        model = torch.load(checkpoint_path, map_location=device)
        model.to(device=device, dtype=precision)
        model.eval()

        # Optional: Compile the model for optimized inference
        if compile:
            model = torch.jit.script(
                model
            )  # or torch.compile(model) if supported in your setup

        # Set up a dummy decode function if needed (replace this with actual decoding if available)
        def decode_one_token(input_token):
            # This is a placeholder; replace with actual token decoding logic
            with torch.no_grad():
                return model(input_token)

        return model, decode_one_token

    def launch_thread_safe_queue(
        self,
        checkpoint_path,
        device,
        precision,
        compile: bool = False,
    ):
        input_queue = queue.Queue()
        init_event = threading.Event()

        def worker():
            model, decode_one_token = self.load_model(
                checkpoint_path, device, precision, compile=compile
            )
            with torch.device(device):
                model.setup_caches(
                    max_batch_size=1,
                    max_seq_len=model.config.max_seq_len,
                    dtype=next(model.parameters()).dtype,
                )
            init_event.set()

            while True:
                item: GenerateRequest | None = input_queue.get()
                if item is None:
                    break

                kwargs = item.request
                response_queue = item.response_queue

                try:
                    for chunk in generate_long(
                        model=model, decode_one_token=decode_one_token, **kwargs
                    ):
                        response_queue.put(
                            WrappedGenerateResponse(status="success", response=chunk)
                        )
                except Exception as e:
                    response_queue.put(
                        WrappedGenerateResponse(status="error", response=e)
                    )

        threading.Thread(target=worker, daemon=True).start()
        init_event.wait()

        return input_queue

    async def get_available_voices(self) -> List[str]:
        """Return list of available voice IDs based on .wav files."""
        try:
            return [
                os.path.splitext(f)[0]
                for f in os.listdir(self.voices_dir)
                if f.endswith(".wav")
            ]
        except Exception as e:
            self.logger.error(f"Error getting available voices: {e}")
            raise

    async def generate_audio(
        self, text: str, voice_id: str
    ) -> Tuple[AudioSegment, str]:
        self.logger.debug(f"Generating audio for text: {text}, voice: {voice_id}")
        try:
            # Create a response queue
            response_queue = queue.Queue()

            # Prepare the request parameters
            request_params = {
                "device": self.device,
                "max_new_tokens": self.max_length,
                "text": text,
                "top_p": 0.95,
                "repetition_penalty": 1.2,
                "temperature": 0.7,
                "iterative_prompt": False,
                "chunk_length": 0,
                "max_length": 2048,
                "prompt_tokens": self._load_reference_tokens(voice_id),
                "prompt_text": "",
            }

            # Create a GenerateRequest object
            generate_request = GenerateRequest(
                request=request_params,
                response_queue=response_queue,
            )

            # Put the request into the input queue
            self.llama_queue.put(generate_request)

            # Create a thread pool executor
            executor = concurrent.futures.ThreadPoolExecutor()

            # Collect responses
            segments = []

            while True:
                # Use the executor to run the blocking get() call
                result: WrappedGenerateResponse = (
                    await asyncio.get_event_loop().run_in_executor(
                        executor, response_queue.get
                    )
                )
                if result.status == "error":
                    self.logger.error(f"Error in inference: {result.response}")
                    raise Exception(f"Error in inference: {result.response}")

                response: GenerateResponse = result.response
                if response.action == "next":
                    break

                # Decode VQ tokens into audio waveform
                fake_audios = self.decode_vq_tokens(
                    decoder_model=self.decoder_model,
                    codes=response.codes,
                )

                fake_audios = fake_audios.float().cpu().numpy()
                segments.append(fake_audios)

            # Concatenate all audio segments
            if len(segments) == 0:
                self.logger.error("No audio generated")
                raise Exception("No audio generated")
            else:
                audio = np.concatenate(segments, axis=1)  # Concatenate along time axis

                # Save the waveform to a temporary WAV file
                temp_wav_file = "temp_output.wav"
                sf.write(temp_wav_file, audio.T, self.decoder_model.sample_rate)
                self.logger.info(f"Saved raw waveform to {temp_wav_file}")

                # Load the audio segment from the WAV file
                audio_segment = AudioSegment.from_wav(temp_wav_file)
                self.logger.debug(
                    f"Audio segment duration: {len(audio_segment)} milliseconds"
                )

                # Optionally, remove the temporary file
                # os.remove(temp_wav_file)

                return audio_segment

        except Exception as e:
            self.logger.error(f"Error in generate_audio: {e}")
            raise

    @torch.inference_mode()
    def inference(
        self,
        text: str,
        enable_reference_audio: bool = False,
        prompt_tokens: Optional[np.ndarray] = None,
        max_new_tokens: int = 200,
        chunk_length: int = 200,
        top_p: float = 0.7,
        repetition_penalty: float = 1.2,
        temperature: float = 0.7,
        seed: str = "0",
    ) -> Generator[
        Tuple[Optional[bytes], Tuple[int, np.ndarray], Optional[str]], None, None
    ]:
        """Generate audio using Fish TTS model."""
        if int(seed) != 0:
            torch.manual_seed(int(seed))

        from .fish_speech.tools.llama.generate import (
            GenerateRequest,
            GenerateResponse,
            WrappedGenerateResponse,
        )

        request = dict(
            device=self.device,
            max_new_tokens=max_new_tokens,
            text=text,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            temperature=temperature,
            iterative_prompt=chunk_length > 0,
            chunk_length=chunk_length,
            max_length=self.max_length,
            prompt_tokens=prompt_tokens if enable_reference_audio else None,
        )

        response_queue = queue.Queue()
        self.llama_queue.put(
            GenerateRequest(
                request=request,
                response_queue=response_queue,
            )
        )

        segments = []
        while True:
            result: WrappedGenerateResponse = response_queue.get()
            if result.status == "error":
                yield None, None, f"Error in inference: {result.response}"
                break

            result: GenerateResponse = result.response
            if result.action == "next":
                break

            # Generate audio from tokens
            fake_audios = self.decode_vq_tokens(
                decoder_model=self.decoder_model,
                codes=result.codes,
            )
            fake_audios = fake_audios.float().cpu().numpy()
            segments.append(fake_audios)

        # Return concatenated audio segments
        if not segments:
            yield None, None, "No audio generated"
        else:
            audio = np.concatenate(segments, axis=0)
            yield None, (24000, audio), None  # 24 kHz sample rate
