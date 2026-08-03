FROM python:3.9

# Force stdout/stderr to be unbuffered — without this, print() output sits in a
# buffer instead of reaching Railway's log collector immediately, since stdout
# isn't a TTY inside the container. Critical for a long-sleeping loop like
# run-loop, where logs would otherwise only flush once the buffer fills or the
# process exits.
ENV PYTHONUNBUFFERED=1

COPY . /home
WORKDIR /home

RUN pip3 install -r requirements.txt --no-cache-dir
