FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    make \
    wget \
    libusb-1.0-0-dev \
    && rm -rf /var/lib/apt/lists/*

RUN wget https://github.com/mccdaq/uldaq/releases/download/v1.2.1/libuldaq-1.2.1.tar.bz2 \
    && tar -xjf libuldaq-1.2.1.tar.bz2 \
    && cd libuldaq-1.2.1 \
    && ./configure && make && make install && ldconfig

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

CMD ["python", "app/main.py"]
