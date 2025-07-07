import os
import re
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional

import discord
from discord import ui
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import IndexModel
from beanie import Document, init_beanie
from pydantic import Field
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --- Database Model ---
class TypologyDefinition(Document):
    term: str
    text: str
    author_id: int
    author_name: str
    reference: str = Field(default="")
    votes: int = Field(default=0)
    voters: List[int] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    
    class Settings:
        name = "typology_definitions"
        indexes = [
            IndexModel([("term", "text")]),  # Search index
            IndexModel([("term", -1), ("votes", -1)]),  # Popularity sorting
            IndexModel([("voters", 1)]),  # Vote tracking
            IndexModel([("author_id", 1)])  # Author lookup
        ]

# --- Interactive UI ---
class DefinitionView(ui.View):
    def __init__(self, definitions: List[TypologyDefinition], term: str, author_id: int):
        super().__init__(timeout=120.0)
        self.definitions = definitions
        self.term = term
        self.author_id = author_id
        self.page = 0
        self.per_page = 5
        self.message = None

    async def update_embed(self, interaction: Optional[discord.Interaction] = None):
        start = self.page * self.per_page
        page_defs = self.definitions[start:start+self.per_page]
        
        embed = discord.Embed(
            title=f"📖 {self.term} Definitions (Page {self.page + 1})",
            color=0x6A0DAD,
            timestamp=datetime.utcnow()
        )
        
        for idx, defn in enumerate(page_defs, 1):
            embed.add_field(
                name=f"#{start + idx} (⭐ {defn.votes})",
                value=(
                    f"{defn.text}\n\n"
                    f"↳ *By {defn.author_name}*\n"
                    f"↳ *Reference: {defn.reference or 'None'}*\n"
                    f"↳ *Updated {discord.utils.format_dt(defn.last_updated, style='R')}*"
                ),
                inline=False
            )
        
        if interaction:
            await interaction.response.edit_message(embed=embed, view=self)
        elif self.message:
            await self.message.edit(embed=embed, view=self)

    @ui.button(emoji="⬅️", style=discord.ButtonStyle.blurple)
    async def prev_page(self, interaction: discord.Interaction, button: ui.Button):
        self.page = max(0, self.page - 1)
        await self.update_embed(interaction)

    @ui.button(emoji="➡️", style=discord.ButtonStyle.blurple)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        if (self.page + 1) * self.per_page < len(self.definitions):
            self.page += 1
            await self.update_embed(interaction)

    @ui.button(emoji="⭐", style=discord.ButtonStyle.green)
    async def upvote(self, interaction: discord.Interaction, button: ui.Button):
        definition_idx = self.page * self.per_page
        if definition_idx >= len(self.definitions):
            return
            
        definition = self.definitions[definition_idx]
        
        if interaction.user.id in definition.voters:
            await interaction.response.send_message("❌ You've already voted!", ephemeral=True)
            return
            
        definition.voters.append(interaction.user.id)
        definition.votes += 1
        definition.last_updated = datetime.utcnow()
        await definition.save()
        
        await interaction.response.send_message("✅ Vote recorded!", ephemeral=True)
        await self.update_embed()

    @ui.button(emoji="✏️", style=discord.ButtonStyle.gray)
    async def edit_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ You can only edit your own definitions", ephemeral=True)
            return
            
        definition_idx = self.page * self.per_page
        definition = self.definitions[definition_idx]
        
        modal = EditModal(definition)
        await interaction.response.send_modal(modal)

    @ui.button(emoji="🗑️", style=discord.ButtonStyle.red)
    async def delete_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ You can only delete your own definitions", ephemeral=True)
            return
            
        definition_idx = self.page * self.per_page
        definition = self.definitions[definition_idx]
        
        await definition.delete()
        self.definitions.pop(definition_idx)
        
        if not self.definitions:
            await interaction.response.edit_message(content=f"✅ All definitions for {self.term} deleted", embed=None, view=None)
            return
            
        await interaction.response.send_message("✅ Definition deleted", ephemeral=True)
        await self.update_embed()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            await self.message.edit(view=self)

class EditModal(ui.Modal, title="Edit Definition"):
    new_text = ui.TextInput(label="New Definition Text", style=discord.TextStyle.long)
    
    def __init__(self, definition: TypologyDefinition):
        super().__init__()
        self.definition = definition
        self.new_text.default = definition.text
    
    async def on_submit(self, interaction: discord.Interaction):
        self.definition.text = str(self.new_text)
        self.definition.last_updated = datetime.utcnow()
        await self.definition.save()
        await interaction.response.send_message("✅ Definition updated!", ephemeral=True)

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
    
    # Manual index conflict resolution
    db = client[os.getenv("MONGO_DB", "typology_bot")]
    collection = db["typology_definitions"]
    
    existing_indexes = await collection.index_information()
    
    # Create only missing indexes
    for index in TypologyDefinition.Settings.indexes:
        index_keys = tuple(index.document["key"].items())
        exists = False
        
        for existing_info in existing_indexes.values():
            if existing_info["key"] == list(index_keys):
                exists = True
                break
                
        if not exists:
            await collection.create_indexes([index])

# --- Commands ---
@bot.event
async def on_ready():
    try:
        await init_db()
        print(f"✅ Bot ready as {bot.user}")
    except Exception as e:
        print(f"❌ Database initialization failed: {str(e)}")
        raise

@bot.command()
async def define(ctx, term: str):
    """View definitions with interactive controls"""
    try:
        definitions = await TypologyDefinition.find(
            TypologyDefinition.term == term.upper()
        ).sort(-TypologyDefinition.votes).to_list()
        
        if not definitions:
            await ctx.send(f"❌ No definitions found for {term.upper()}")
            return
        
        view = DefinitionView(definitions, term.upper(), ctx.author.id)
        view.message = await ctx.send(
            embed=discord.Embed(title=f"Loading {term.upper()}...", color=0x6A0DAD),
            view=view
        )
        await view.update_embed()
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")

@bot.command()
async def add(ctx, term: str, *, definition: str):
    """Add a new definition"""
    try:
        if len(definition) > 2000:
            await ctx.send("❌ Definition too long (max 2000 chars)")
            return
        
        new_def = TypologyDefinition(
            term=term.upper(),
            text=definition,
            author_id=ctx.author.id,
            author_name=str(ctx.author),
            voters=[ctx.author.id]  # Auto-upvote
        )
        await new_def.insert()
        await ctx.send(f"✅ Added definition for {term.upper()}!")
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")

@bot.command()
async def search(ctx, *, query: str):
    """Search across all definitions"""
    try:
        results = await TypologyDefinition.find(
            {"$text": {"$search": query}}
        ).sort(-TypologyDefinition.votes).limit(5).to_list()
        
        if not results:
            await ctx.send("🔍 No results found")
            return
        
        embed = discord.Embed(title=f"🔍 Search Results", color=0x3498DB)
        for result in results:
            embed.add_field(
                name=f"{result.term} (⭐ {result.votes})",
                value=f"{result.text[:150]}...",
                inline=False
            )
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Search error: {str(e)}")

# --- Run Bot ---
if __name__ == "__main__":
    required_vars = ["DISCORD_TOKEN", "MONGO_URI"]
    if missing := [var for var in required_vars if not os.getenv(var)]:
        print(f"❌ Missing env vars: {', '.join(missing)}")
        exit(1)
    
    bot.run(os.getenv("DISCORD_TOKEN"))