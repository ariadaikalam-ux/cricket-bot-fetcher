FROM mcr.microsoft.com/playwright:v1.42.1-jammy

RUN apt-get update && apt-get install -y python3 python3-pip && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install --omit=dev

COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "bot.py"]
