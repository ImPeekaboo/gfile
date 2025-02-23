import concurrent.futures
import functools
import io
import math
import re
import time
import uuid
from datetime import datetime
from os import rename
from pathlib import Path
from subprocess import run

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from requests_toolbelt import MultipartEncoder, StreamingIterator
from tqdm import tqdm
from urllib3.util.retry import Retry


def bytes_to_size_str(bytes):
   if bytes == 0:
       return "0B"
   units = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
   i = int(math.floor(math.log(bytes, 1024)))
   p = math.pow(1024, i)
   return f"{bytes/p:.02f} {units[i]}"


def size_str_to_bytes(size_str):
    if isinstance(size_str, int):
        return size_str
    m = re.search(r'^(?P<num>\d+) ?((?P<unit>[KMGTPEZY]?)(iB|B)?)$', size_str, re.IGNORECASE)
    assert m
    units = ("B", "K", "M", "G", "T", "P", "E", "Z", "Y")
    unit = (m['unit'] or 'B').upper()
    return int(math.pow(1024, units.index(unit)) * int(m['num']))


def requests_retry_session(
    retries=5,
    backoff_factor=0.2,
    status_forcelist=None, # (500, 502, 504)
    session=None,
):
    session = session or requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def split_file(input_file, out, target_size=None, start=0, chunk_copy_size=1024*1024):
    input_file = Path(input_file)
    size = 0

    input_size = input_file.stat().st_size
    if target_size is None:
        output_size = input_size - start
    else:
        output_size = min( target_size, input_size - start)

    with open(input_file, 'rb') as f:
        f.seek(start)
        while True:
            # print(f'{size / output_size * 100:.2f}%', end='\r')
            if size == output_size: break
            if size > output_size:
                raise Exception(f'Size ({size}) is larger than {target_size} bytes!')
            current_chunk_size = min(chunk_copy_size, output_size - size)
            chunk = f.read(current_chunk_size)
            if not chunk: break
            size += len(chunk)
            out.write(chunk)


class GFile:
    def __init__(self, uri, progress=False, thread_num=4, chunk_size=1024*1024*10, chunk_copy_size=1024*1024, timeout=10, password=None, aria2=False, **kwargs) -> None:
        self.uri = uri
        self.chunk_size = size_str_to_bytes(chunk_size)
        self.chunk_copy_size = size_str_to_bytes(chunk_copy_size)
        self.thread_num=thread_num
        self.progress = progress
        self.data = None
        self.pbar = None
        self.timeout = timeout
        self.session = requests_retry_session()
        self.session.request = functools.partial(self.session.request, timeout=self.timeout)
        self.cookies = None
        self.current_chunk = 0
        self.password = password
        self.aria2 = aria2


    def upload_chunk(self, chunk_no, chunks):
        bar = self.pbar[chunk_no % self.thread_num] if self.pbar else None
        with io.BytesIO() as f:
            split_file(self.uri, f, self.chunk_size, start=chunk_no * self.chunk_size, chunk_copy_size=self.chunk_copy_size)
            chunk_size = f.tell()
            f.seek(0)
            fields = {
                "id": self.token,
                "name": Path(self.uri).name,
                "chunk": str(chunk_no),
                "chunks": str(chunks),
                "lifetime": "100",
                "file": ("blob", f, "application/octet-stream"),
            }
            form_data = MultipartEncoder(fields)
            headers = {
                "content-type": form_data.content_type,
            }
            # convert the form-data into a binary string, this way we can control/throttle its read() behavior
            form_data_binary = form_data.to_string()
            del form_data

        size = len(form_data_binary)
        if bar:
            bar.desc = f'chunk {chunk_no + 1}/{chunks}'
            bar.reset(total=size)
            # bar.refresh()

        def gen():
            offset = 0
            while True:
                if offset < size:
                    update_tick = 1024 * 128
                    yield form_data_binary[offset:offset+update_tick]
                    if bar:
                        bar.update(min(update_tick, size - offset))
                        bar.refresh()
                    offset += update_tick
                else:
                    if chunk_no != self.current_chunk:
                        time.sleep(0.01)
                    else:
                        time.sleep(0.1)
                        break
        while True:
            try:
                streamer = StreamingIterator(size, gen())
                resp = self.session.post(f"https://{self.server}/upload_chunk.php", data=streamer, headers=headers)
            except Exception as ex:
                print(ex)
                print('Retrying...')
            else:
                break

        resp_data = resp.json()
        self.current_chunk += 1

        if 'url' in resp_data:
            self.data = resp_data
        if 'status' not in resp_data or resp_data['status']:
            print(resp_data)
            self.failed = True


    def upload(self):
        self.token = uuid.uuid1().hex
        self.pbar = None
        self.failed = False
        assert Path(self.uri).exists()
        size = Path(self.uri).stat().st_size
        chunks = math.ceil(size / self.chunk_size)
        print(f'Filesize {bytes_to_size_str(size)}, chunk size: {bytes_to_size_str(self.chunk_size)}, total chunks: {chunks}')

        if self.progress:
            self.pbar = []
            for i in range(self.thread_num):
                self.pbar.append(tqdm(total=size, unit="B", unit_scale=True, leave=False, unit_divisor=1024, ncols=100, position=i))

        self.server = re.search(r'var server = "(.+?)"', self.session.get('https://gigafile.nu/').text)[1]

        # upload the first chunk to set cookies properly.
        self.upload_chunk(0, chunks)

        # upload second to second last chunk(s)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.thread_num) as ex:
            futures = {ex.submit(self.upload_chunk, i, chunks): i for i in range(1, chunks)}
            try:
                for future in concurrent.futures.as_completed(futures):
                    if self.failed:
                        print('Failed!')
                        for future in futures:
                            future.cancel()
                        return
            except KeyboardInterrupt:
                print('\nUser cancelled the operation.')
                for future in futures:
                    future.cancel()
                return

        # upload last chunk if not already
        # if chunks > 1:
        #     # print('\nupload the last chunk in single thread')
        #     self.upload_chunk(chunks - 1, chunks)

        if self.pbar:
            for bar in self.pbar:
                bar.close()
        print('')
        if 'url' not in self.data:
            print('Something went wrong. Upload failed.', self.data)
        return self # for chain


    def get_download_page(self):
        if not self.data or not 'url' in self.data:
            return
        f = Path(self.uri)
        print(f"Finished at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, filename: {f.name}, size: {bytes_to_size_str(f.stat().st_size)}")
        print(self.data['url'])
        return self.data['url']


    def download(self, filename=None):
        m = re.search(r'^https?:\/\/\d+?\.gigafile\.nu\/([a-z0-9-]+)$', self.uri)
        if not m:
            print('Invalid URL.')
            return
        r = self.session.get(self.uri) # setup cookie
        try:
            soup = BeautifulSoup(r.text, 'html.parser')
            if soup.select_one('#contents_matomete'):
                print('Matomete mode. Getting info of first file (currently only support one file)...')
                ele = soup.select_one('.matomete_file')
                web_name = ele.select_one('.matomete_file_info > span:nth-child(2)').text.strip()
                file_id = re.search(r'download\(\d+, *\'(.+?)\'', ele.select_one('.download_panel_btn_dl')['onclick'])[1]
                size_str = re.search(r'（(.+?)）', ele.select_one('.matomete_file_info > span:nth-child(3)').text.strip())[1]
            else:
                file_id = m[1]
                size_str = soup.select_one('.dl_size').text.strip()
                web_name = soup.select_one('#dl').text.strip()

            print(f'Name: {web_name}, size: {size_str}, id: {file_id}')
        except Exception as ex:
            print(f'ERROR! Failed to parse the page {self.uri}.')
            print(ex)
            print('Please report it back to the developer.')

        if not filename:
            # only sanitize web filename. User provided ones are on their own.
            filename = re.sub(r'[\\/:*?"<>|]', '_', web_name)

        download_url = self.uri.rsplit('/', 1)[0] + '/download.php?file=' + file_id
        if self.password:
            download_url = download_url + '&dlkey=' + self.password

        if self.aria2:
            cookie_str = "; ".join([f"{cookie.name}={cookie.value}" for cookie in self.session.cookies])
            cmd = ['aria2c', download_url, '--header', f'Cookie: {cookie_str}', '-o', filename]
            cmd.extend(self.aria2.split(' '))
            run(cmd)
            return

        temp = filename + '.dl'

        with self.session.get(download_url, stream=True) as r:
            r.raise_for_status()
            filesize = int(r.headers['Content-Length'])
            if self.progress:
                desc = filename if len(filename) <= 20 else filename[0:11] + '..' + filename[-7:]
                self.pbar = tqdm(total=filesize, unit='B', unit_scale=True, unit_divisor=1024, desc=desc)
            with open(temp, 'wb') as f:
                for chunk in r.iter_content(chunk_size=self.chunk_copy_size):
                    f.write(chunk)
                    if self.pbar: self.pbar.update(len(chunk))
        if self.pbar: self.pbar.close()

        filesize_downloaded = Path(temp).stat().st_size
        print(f'Filesize check: expected: {filesize}; actual: {filesize_downloaded}')
        if filesize == filesize_downloaded:
            print("Succeeded.")
            rename(temp, filename)
        else:
            print(f"Downloaded file is corrupt. Please check the broken file at {temp} and delete it yourself if needed.")
        return filename
