# Sources of the media corpus

All files here are public domain.

## speech_de.ogg

- 30.0 s from LibriVox's reading of Franz Kafka, "Die Verwandlung", chapter 1, read in German. The excerpt runs from 24.97 s to 54.97 s of the file and holds the first two sentences.
  - Recording: https://archive.org/details/verwandlung_mw_0811_librivox (file `verwandlung_01_kafka_64kb.mp3`).
  - Licence: public domain (LibriVox; archive.org's licence URL http://creativecommons.org/licenses/publicdomain/).
- Encoding: decoded and resampled to 16 kHz mono with PyAV, then encoded as Opus (libopus, 32 kbit/s) in Ogg. Opus decoders report 48 kHz.
- Expected text (`speech_de.expected.txt`): the same two sentences from Projekt Gutenberg eBook #22367, https://www.gutenberg.org/ebooks/22367 (public domain). The reader says "ungeheuren" where the book has "ungeheueren".

## ../../../src/text_extraction/media/asr_check.ogg

- The first 5.0 s of `speech_de.ogg` ("Als Gregor Samsa eines Morgens"), with the same encoding and source.
