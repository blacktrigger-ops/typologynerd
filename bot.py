import os
import re
from datetime import datetime
from typing import Optional

import discord
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import IndexModel
from beanie import Document, Indexed, init_beanie
from pydantic import Field
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --- Database Model ---
class TypologyDefinition(Document):
    term: str = Indexed(str)
    text: str
    author_id: int
    author_name: str
    reference: str = Field(default="")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Settings:
        name = "typology_definitions"
        indexes = [
            IndexModel([("term", "text")], name="term_text_search"),
            IndexModel([("author_id", 1)], name="author_lookup")
        ]

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Database Connection ---
async def init_db():
    client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
    await init_beanie(
        database=client[os.getenv("MONGO_DB", "typology_bot")],
        document_models=[TypologyDefinition]
    )

# --- Command Implementation ---
@bot.event
async def on_ready():
    await init_db()
    print(f"Logged in as {bot.user}")

@bot.event
async def on_message(message):
    # Process commands first
    await bot.process_commands(message)
    
    # Check if message is a reply to a definition
    if message.reference and message.content.startswith("tp define"):
        await process_definition(message)

async def process_definition(message):
    try:
        # Get the original message containing the definition
        original = await message.channel.fetch_message(message.reference.message_id)
        
        # Parse the command: "tp define TERM @author reference"
        parts = re.split(r'\s+', message.content, maxsplit=3)
        term = parts[2].upper()
        
        # Extract optional author (default to message author)
        author = message.author
        if len(parts) > 3 and message.mentions:
            author = message.mentions[0]
        
        # Extract optional reference (anything after author)
        reference = ""
        if len(parts) > 3:
            reference = message.content.split(maxsplit=3)[3]
            if message.mentions:
                reference = reference.replace(f"<@{message.mentions[0].id}>", "").strip()
        
        # Save to database
        definition = TypologyDefinition(
            term=term,
            text=original.content,
            author_id=author.id,
            author_name=str(author),
            reference=reference
        )
        await definition.insert()
        
        # Send confirmation
        embed = discord.Embed(
            title="✅ Definition Saved",
            description=f"Term: **{term}**",
            color=0x00ff00
        )
        embed.add_field(name="Definition", value=original.content, inline=False)
        embed.add_field(name="Author", value=author.display_name)
        if reference:
            embed.add_field(name="Reference", value=reference)
        embed.set_footer(text=f"Added by {message.author}")
        
        await message.reply(embed=embed)
        
    except Exception as e:
        await message.reply(f"❌ Error: {str(e)}")

@bot.command()
async def define(ctx, term: str):
    """View all definitions for a term"""
    definitions = await TypologyDefinition.find(
        TypologyDefinition.term == term.upper()
    ).sort("-created_at").to_list()
    
    if not definitions:
        await ctx.send(f"No definitions found for **{term}**")
        return
    
    embed = discord.Embed(
        title=f"Definitions for {term.upper()}",
        color=0x6A0DAD
    )
    
    for idx, defn in enumerate(definitions, 1):
        embed.add_field(
            name=f"Definition #{idx}",
            value=(
                f"{defn.text}\n\n"
                f"↳ *By {defn.author_name}*\n"
                f"↳ *Reference: {defn.reference or 'None'}*\n"
                f"↳ *Added {discord.utils.format_dt(defn.created_at, style='R')}*"
            ),
            inline=False
        )
    
    await ctx.send(embed=embed)

# --- Run Bot ---
if __name__ == "__main__":
    required_vars = ["DISCORD_TOKEN", "MONGO_URI"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        print(f"Missing environment variables: {', '.join(missing_vars)}")
        exit(1)
    
    bot.run(os.getenv("DISCORD_TOKEN"))
