import os
import asyncio
from datetime import datetime
from typing import Optional, List, Dict

import discord
from discord import ui
from discord.ext import commands, menus
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError
from pydantic import BaseModel, Field
from beanie import Document, Indexed, init_beanie
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --- Database Models ---
class Definition(Document):
    term: Indexed(str)
    text: str
    author_id: int
    author_name: str
    reference: str = ""
    votes: int = 0
    voters: List[int] = Field(default_factory=list)  # Track voters for persistence
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    
    class Settings:
        name = "definitions"
        indexes = [
            [("term", "text"), {"name": "term_text_search"}],
            [("term", "-votes"), {"name": "term_popularity"}]
        ]

    async def add_vote(self, user_id: int, value: int) -> bool:
        """Add vote if user hasn't voted before"""
        if user_id in self.voters:
            return False
            
        self.voters.append(user_id)
        self.votes += value
        self.last_updated = datetime.utcnow()
        await self.save()
        return True

# --- Interactive UI Components ---
class DefinitionView(ui.View):
    def __init__(self, definitions: List[Definition], term: str, page: int = 0):
        super().__init__(timeout=60.0)
        self.definitions = definitions
        self.term = term
        self.page = page
        self.total_pages = (len(definitions) + 4) // 5  # 5 per page
        self.message = None

    async def update_embed(self, interaction: discord.Interaction):
        """Update the embed with current page"""
        start_idx = self.page * 5
        end_idx = start_idx + 5
        current_defs = self.definitions[start_idx:end_idx]
        
        embed = discord.Embed(
            title=f"📚 {self.term.upper()} Definitions",
            color=0x6A0DAD,
            timestamp=datetime.utcnow()
        )
        
        for idx, definition in enumerate(current_defs, start=1):
            embed.add_field(
                name=f"#{start_idx + idx} (⭐ {definition.votes})",
                value=(
                    f"{definition.text}\n\n"
                    f"↳ *By {definition.author_name}*\n"
                    f"↳ *Added {discord.utils.format_dt(definition.created_at, style='R')}*"
                ),
                inline=False
            )
        
        embed.set_footer(
            text=f"Page {self.page + 1}/{self.total_pages} | 📩 Add your own with !add"
        )
        
        # Update buttons state
        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page == self.total_pages - 1
        
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(emoji="⬅️", style=discord.ButtonStyle.grey)
    async def prev_page(self, interaction: discord.Interaction, button: ui.Button):
        self.page = max(0, self.page - 1)
        await self.update_embed(interaction)

    @ui.button(emoji="➡️", style=discord.ButtonStyle.grey)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        self.page = min(self.total_pages - 1, self.page + 1)
        await self.update_embed(interaction)

    @ui.button(emoji="⭐", style=discord.ButtonStyle.blurple)
    async def upvote(self, interaction: discord.Interaction, button: ui.Button):
        """Handle voting with persistent tracking"""
        definition = self.definitions[self.page * 5]  # First definition on page
        
        if interaction.user.id in definition.voters:
            await interaction.response.send_message(
                "You've already voted on this definition!",
                ephemeral=True
            )
            return
            
        success = await definition.add_vote(interaction.user.id, 1)
        if success:
            await interaction.response.send_message(
                "Thanks for voting! ⭐",
                ephemeral=True
            )
            await self.update_embed(interaction)
        else:
            await interaction.response.send_message(
                "Couldn't process your vote",
                ephemeral=True
            )

    async def on_timeout(self):
        """Disable buttons when view times out"""
        for item in self.children:
            item.disabled = True
        if self.message:
            await self.message.edit(view=self)

# --- Bot Configuration ---
class Config(BaseModel):
    TOKEN: str
    MONGO_URI: str
    MONGO_DB: str = "typology_bot"
    COMMAND_PREFIX: str = "!"
    BOT_COLOR: int = 0x6A0DAD
    PAGE_SIZE: int = 5
    VOTE_COOLDOWN: int = 60

# --- Bot Implementation ---
class TypologyBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        
        super().__init__(
            command_prefix=config.COMMAND_PREFIX,
            intents=intents,
            help_command=None
        )
        
        self.config = config
        self.mongo_client = None
        self.db = None
        self.ready = False
        
    async def setup_hook(self):
        """Initialize database connection"""
        self.mongo_client = AsyncIOMotorClient(self.config.MONGO_URI)
        await init_beanie(
            database=self.mongo_client[self.config.MONGO_DB],
            document_models=[Definition]
        )
        self.ready = True
        print("Database connection established")

    async def close(self):
        """Cleanup on bot shutdown"""
        if self.mongo_client:
            self.mongo_client.close()
        await super().close()

# --- Bot Instance ---
bot = TypologyBot(Config(
    TOKEN=os.getenv("DISCORD_TOKEN"),
    MONGO_URI=os.getenv("MONGO_URI"),
    MONGO_DB=os.getenv("MONGO_DB", "typology_bot")
))

# --- Commands ---
@bot.command()
async def define(ctx, term: str):
    """View definitions with interactive controls"""
    if not bot.ready:
        return await ctx.send("🔄 Bot is initializing, please wait...")
    
    definitions = await Definition.find(
        Definition.term == term.upper()
    ).sort(-Definition.votes).to_list()
    
    if not definitions:
        embed = discord.Embed(
            title=f"No definitions found for {term.upper()}",
            description=f"Use `{bot.config.COMMAND_PREFIX}add {term} [definition]` to add one!",
            color=0xE74C3C
        )
        return await ctx.send(embed=embed)
    
    view = DefinitionView(definitions, term)
    view.message = await ctx.send(
        embed=discord.Embed(
            title=f"Loading {term.upper()} definitions...",
            color=bot.config.BOT_COLOR
        ),
        view=view
    )
    await view.update_embed(ctx.interaction if ctx.interaction else None)

@bot.command()
async def add(ctx, term: str, *, definition: str):
    """Add a new definition"""
    if not bot.ready:
        return await ctx.send("🔄 Bot is initializing, please wait...")
    
    if len(definition) > 1000:
        return await ctx.send("❌ Definition too long (max 1000 characters)")
    
    try:
        new_def = await Definition(
            term=term.upper(),
            text=definition,
            author_id=ctx.author.id,
            author_name=str(ctx.author)
        ).insert()
        
        embed = discord.Embed(
            title=f"✅ New definition added for {term.upper()}",
            description=new_def.text,
            color=0x2ECC71
        )
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        embed.set_footer(text=f"Use !define {term} to view all definitions")
        await ctx.send(embed=embed)
        
    except DuplicateKeyError:
        await ctx.send("❌ This definition already exists!")

@bot.command()
async def search(ctx, *, query: str):
    """Search across all definitions"""
    if not bot.ready:
        return await ctx.send("🔄 Bot is initializing, please wait...")
    
    # Create text index if not exists
    await Definition.get_motor_collection().create_index([("text", "text")])
    
    results = await Definition.find(
        {"$text": {"$search": query}}
    ).sort(-Definition.votes).limit(5).to_list()
    
    if not results:
        return await ctx.send("🔍 No results found for your search")
    
    embed = discord.Embed(
        title=f"🔍 Search Results for '{query}'",
        color=0x3498DB
    )
    
    for result in results:
        embed.add_field(
            name=f"{result.term.upper()} (⭐ {result.votes})",
            value=f"{result.text[:150]}...\n[View all]({ctx.message.jump_url})",
            inline=False
        )
    
    await ctx.send(embed=embed)

# --- Error Handling ---
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        embed = discord.Embed(
            title="❌ Missing Argument",
            description=f"Usage: `{ctx.prefix}{ctx.command.name} {ctx.command.signature}`",
            color=0xE74C3C
        )
        await ctx.send(embed=embed)
    else:
        print(f"Error in {ctx.command}: {error}")
        embed = discord.Embed(
            title="⚠️ An error occurred",
            description=str(error),
            color=0xF39C12
        )
        await ctx.send(embed=embed)

# --- Startup ---
if __name__ == "__main__":
    required_vars = ["DISCORD_TOKEN", "MONGO_URI"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        print(f"❌ Missing environment variables: {', '.join(missing_vars)}")
        exit(1)
    
    bot.run(os.getenv("DISCORD_TOKEN"))