import os
import asyncio
from datetime import datetime
from typing import List, Optional

import discord
from discord import ui
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import IndexModel
from beanie import Document, Indexed, init_beanie
from pydantic import Field  # This was missing
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --- Database Models ---
class PersistentDefinition(Document):
    term: str = Indexed(str)
    text: str
    author_id: int
    author_name: str
    reference: str = Field(default="")  # Now properly using Field
    votes: int = Field(default=0)
    voters: List[int] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    
    class Settings:
        name = "typology_definitions"
        indexes = [
            IndexModel([("term", "text")], name="term_text_search"),
            IndexModel([("term", -1), ("votes", -1)], name="term_popularity"),
            IndexModel([("voters", 1)], name="voter_lookup")
        ]

    async def add_vote(self, user_id: int, value: int) -> bool:
        """Persistent voting with duplicate prevention"""
        if user_id in self.voters:
            return False
            
        self.voters.append(user_id)
        self.votes += value
        self.last_updated = datetime.utcnow()
        await self.save()
        return True

# --- Interactive UI Components ---
class DefinitionPaginator(ui.View):
    def __init__(self, definitions: List[PersistentDefinition], term: str):
        super().__init__(timeout=120.0)
        self.definitions = definitions
        self.term = term
        self.page = 0
        self.per_page = 5
        self.total_pages = (len(definitions) + self.per_page - 1) // self.per_page
        self.message = None

    async def update_embed(self, interaction: Optional[discord.Interaction] = None):
        """Update embed with current page data"""
        start = self.page * self.per_page
        end = start + self.per_page
        page_defs = self.definitions[start:end]
        
        embed = discord.Embed(
            title=f"📖 {self.term.upper()} Definitions",
            color=0x6A0DAD,
            timestamp=datetime.utcnow()
        )
        
        for idx, definition in enumerate(page_defs, start=1):
            embed.add_field(
                name=f"#{start + idx} (⭐ {definition.votes})",
                value=(
                    f"{definition.text}\n\n"
                    f"↳ *By {definition.author_name}*\n"
                    f"↳ *Last updated {discord.utils.format_dt(definition.last_updated, style='R')}*"
                ),
                inline=False
            )
        
        embed.set_footer(text=f"Page {self.page + 1}/{self.total_pages}")
        
        # Update button states
        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page == self.total_pages - 1
        
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
        self.page = min(self.total_pages - 1, self.page + 1)
        await self.update_embed(interaction)

    @ui.button(emoji="⭐", style=discord.ButtonStyle.green)
    async def upvote(self, interaction: discord.Interaction, button: ui.Button):
        """Handle persistent voting"""
        definition_idx = self.page * self.per_page
        if definition_idx >= len(self.definitions):
            return
            
        definition = self.definitions[definition_idx]
        
        if interaction.user.id in definition.voters:
            await interaction.response.send_message(
                "You've already voted on this definition!",
                ephemeral=True
            )
            return
            
        if await definition.add_vote(interaction.user.id, 1):
            await interaction.response.send_message(
                "Vote recorded! ⭐",
                ephemeral=True
            )
            # Refresh the definitions list to show updated votes
            self.definitions = await PersistentDefinition.find(
                PersistentDefinition.term == self.term
            ).sort(-PersistentDefinition.votes).to_list()
            await self.update_embed()
        else:
            await interaction.response.send_message(
                "Failed to record vote",
                ephemeral=True
            )

    async def on_timeout(self):
        """Disable buttons when view times out"""
        for item in self.children:
            item.disabled = True
        if self.message:
            await self.message.edit(view=self)

# --- Bot Configuration ---
class TypologyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        
        super().__init__(
            command_prefix=os.getenv("COMMAND_PREFIX", "!"),
            intents=intents,
            help_command=None
        )
        
        self.mongo_client = None
        self.ready = False
        
    async def setup_hook(self):
        """Initialize database connection"""
        try:
            self.mongo_client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
            await init_beanie(
                database=self.mongo_client[os.getenv("MONGO_DB", "typology_bot")],
                document_models=[PersistentDefinition]
            )
            self.ready = True
            print("✅ Database connection established")
        except Exception as e:
            print(f"❌ Database connection failed: {e}")
            raise

    async def close(self):
        """Cleanup on bot shutdown"""
        if self.mongo_client:
            self.mongo_client.close()
        await super().close()

# --- Bot Instance ---
bot = TypologyBot()

# --- Commands ---
@bot.command()
async def define(ctx, term: str):
    """View persistent definitions with interactive controls"""
    if not bot.ready:
        return await ctx.send("🔄 Bot is initializing, please wait...")
    
    try:
        definitions = await PersistentDefinition.find(
            PersistentDefinition.term == term.upper()
        ).sort(-PersistentDefinition.votes).to_list()
        
        if not definitions:
            embed = discord.Embed(
                title=f"No persistent definitions found for {term.upper()}",
                description=f"Use `{bot.command_prefix}add {term} [definition]` to add one!",
                color=0xE74C3C
            )
            return await ctx.send(embed=embed)
        
        view = DefinitionPaginator(definitions, term)
        view.message = await ctx.send(
            embed=discord.Embed(
                title=f"Loading {term.upper()} definitions...",
                color=0x6A0DAD
            ),
            view=view
        )
        await view.update_embed()
    except Exception as e:
        await ctx.send(f"❌ Error loading definitions: {str(e)}")

@bot.command()
async def add(ctx, term: str, *, definition: str):
    """Add a new persistent definition"""
    if not bot.ready:
        return await ctx.send("🔄 Bot is initializing, please wait...")
    
    try:
        if len(definition) > 1000:
            return await ctx.send("❌ Definition too long (max 1000 characters)")
        
        new_def = PersistentDefinition(
            term=term.upper(),
            text=definition,
            author_id=ctx.author.id,
            author_name=str(ctx.author),
            voters=[ctx.author.id]  # Auto-upvote
        )
        await new_def.insert()
        
        embed = discord.Embed(
            title=f"✅ Persistent definition added for {term.upper()}",
            description=new_def.text,
            color=0x2ECC71
        )
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        embed.set_footer(text=f"Use {bot.command_prefix}define {term} to view")
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Error adding definition: {str(e)}")

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
    # Verify required environment variables
    required_vars = ["DISCORD_TOKEN", "MONGO_URI"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        print(f"❌ Missing environment variables: {', '.join(missing_vars)}")
        exit(1)
    
    try:
        bot.run(os.getenv("DISCORD_TOKEN"))
    except Exception as e:
        print(f"❌ Bot crashed: {e}")
