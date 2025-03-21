import subprocess


cmd = 'ffmpeg -f image2 -r 30 -i /home/ryu-yang/.cache/huggingface/lerobot/ryu-yang/adora_test/images/observation.images.top/episode_000000/frame_%06d.png -vcodec libopenh264 -pix_fmt yuv420p -g 10 -crf 10 -loglevel error -y /home/ryu-yang/.cache/huggingface/lerobot/ryu-yang/adora_test/videos/chunk-000/observation.images.top/episode_000000.mp4'

result = subprocess.run('echo $HOME', shell=True, check=True, stdout=subprocess.PIPE, text=True)
print(result.stdout)

result = subprocess.run('which ffmpeg', shell=True, check=True, stdout=subprocess.PIPE, text=True)
print(result.stdout)

result = subprocess.run(cmd, shell=True, check=True, stdout=subprocess.PIPE, text=True)
print(result.stdout)
