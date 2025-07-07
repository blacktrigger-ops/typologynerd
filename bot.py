
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
    votes: int = Field(default=0)
    voters: List[int] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    
    class Settings:
        name = "typology_definitions"
        indexes = [
            IndexModel([("term", "text")], name="term_text_search"),
            IndexModel([("term", -1), ("votes", -1)], name="term_popularity"),
            IndexModel([("voters", 1)], name="voter_lookup"),
            IndexModel([("author_id", 1)], name="author_definitions")
        ]

# --- Interactive UI ---
class DefinitionPaginator(ui.View):
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
            color=0x6A0DAD
        )
        
        for idx, defn in enumerate(page_defs, 1):
            field_value = f"{defn.text}\n\n⭐ **Votes:** {defn.votes}"
            if defn.reference:
                field_value += f"\n📚 **Reference:** {defn.reference}"
            field_value += f"\n👤 **Author:** {defn.author_name}"
            field_value += f"\n⏱️ **Last Updated:** {discord.utils.format_dt(defn.last_updated, style='R')}"
            
            embed.add_field(
                name=f"Definition #{start + idx}",
                value=field_value,
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

# --- Commands ---
@bot.event
async def on_ready():
    await init_db()
    print(f"✅ Bot ready as {bot.user}")

@bot.command()
async def define(ctx, term: str):
    """View definitions with interactive controls"""
    definitions = await TypologyDefinition.find(
        TypologyDefinition.term == term.upper()
    ).sort(-TypologyDefinition.votes).to_list()
    
    if not definitions:
        await ctx.send(f"❌ No definitions found for {term.upper()}")
        return
    
    view = DefinitionPaginator(definitions, term.upper(), ctx.author.id)
    view.message = await ctx.send(
        embed=discord.Embed(title=f"Loading {term.upper()}...", color=0x6A0DAD),
        view=view
    )
    await view.update_embed()

@bot.command()
async def add(ctx, term: str, *, definition: str):
    """Add a new definition"""
    if len(definition) > 2000:
        await ctx.send("❌ Definition too long (max 2000 chars)")
        return
    
    try:
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

# --- Run Bot ---
if __name__ == "__main__":
    required_vars = ["DISCORD_TOKEN", "MONGO_URI"]
    if missing := [var for var in required_vars if not os.getenv(var)]:
        print(f"❌ Missing env vars: {', '.join(missing)}")
        exit(1)
    
    bot.run(os.getenv("DISCORD_TOKEN"))